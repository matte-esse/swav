# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import argparse
import math
import os
import shutil
import time
from logging import getLogger

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
if "APEX" in os.environ:
    import apex
    from apex.parallel.LARC import LARC

try:
    from .src.utils import (
        get_args,
        bool_flag,
        initialize_exp,
        restart_from_checkpoint,
        fix_random_seeds,
        AverageMeter,
        init_distributed_mode,
    )
    from .src.multicropdataset import MultiCropDataset
    from .src import resnet50 as resnet_models
except ImportError:
    from src.utils import (
        get_args,
        bool_flag,
        initialize_exp,
        restart_from_checkpoint,
        fix_random_seeds,
        AverageMeter,
        init_distributed_mode,
    )
    from src.multicropdataset import MultiCropDataset
    from src import resnet50 as resnet_models

logger = getLogger()

os.environ['RANK'] = '0'
os.environ['WORLD_SIZE'] = '1'
os.environ['MASTER_ADDR'] = 'localhost'
if "MASTER_PORT" not in os.environ:
    os.environ['MASTER_PORT'] = '8080'

parser = argparse.ArgumentParser(description="Implementation of SwAV")

def run_swav(args, train_dataset=None):
    """
    Train a SwAV model

    Args:
        args: SwAV arguments, as provided by
            :func:`slideflow.swav.get_args()`.
        train_loader (torch.utils.data.DataLoader): Data loader for training the
            SwAV model.
    """
    init_distributed_mode(args)
    fix_random_seeds(args.seed)
    logger, training_stats = initialize_exp(args, "epoch", "loss")

    # build data
    if train_dataset is None:
        train_dataset = MultiCropDataset(
            args.data_path,
            args.size_crops,
            args.nmb_crops,
            args.min_scale_crops,
            args.max_scale_crops,
        )
    sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        sampler=sampler,
        batch_size=args.batch_size,
        num_workers=args.workers,
        pin_memory=True,
        drop_last=True
    )
    logger.info("Building data done with {} images loaded.".format(len(train_dataset)))

    # build model
    model = resnet_models.__dict__[args.arch](
        normalize=True,
        hidden_mlp=args.hidden_mlp,
        output_dim=args.feat_dim,
        nmb_prototypes=args.nmb_prototypes,
    )
    # synchronize batch norm layers
    if args.sync_bn == "pytorch":
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    elif args.sync_bn == "apex":
        # with apex syncbn we sync bn per group because it speeds up computation
        # compared to global syncbn
        process_group = apex.parallel.create_syncbn_process_group(args.syncbn_process_group_size)
        model = apex.parallel.convert_syncbn_model(model, process_group=process_group)
    # copy model to GPU
    model = model.cuda()
    if args.rank == 0:
        logger.info(model)
    logger.info("Building model done.")

    # build optimizer
    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=args.base_lr,
        momentum=0.9,
        weight_decay=args.wd,
    )
    if "APEX" in os.environ:
        optimizer = LARC(optimizer=optimizer, trust_coefficient=0.001, clip=False)
    warmup_lr_schedule = np.linspace(args.start_warmup, args.base_lr, len(train_loader) * args.warmup_epochs)
    iters = np.arange(len(train_loader) * (args.epochs - args.warmup_epochs))
    cosine_lr_schedule = np.array([args.final_lr + 0.5 * (args.base_lr - args.final_lr) * (1 + \
                         math.cos(math.pi * t / (len(train_loader) * (args.epochs - args.warmup_epochs)))) for t in iters])
    lr_schedule = np.concatenate((warmup_lr_schedule, cosine_lr_schedule))
    logger.info("Building optimizer done.")

    # init mixed precision
    if args.use_fp16:
        model, optimizer = apex.amp.initialize(model, optimizer, opt_level="O1")
        logger.info("Initializing mixed precision done.")

    # wrap model
    model = nn.parallel.DistributedDataParallel(
        model,
        device_ids=[args.gpu_to_work_on]
    )

    # optionally resume from a checkpoint
    to_restore = {"epoch": 0}
    if "APEX" in os.environ:
        restart_from_checkpoint(
            os.path.join(args.dump_path, "checkpoint.pth.tar"),
            run_variables=to_restore,
            state_dict=model,
            optimizer=optimizer,
            amp=apex.amp,
        )
    else:
        restart_from_checkpoint(
            os.path.join(args.dump_path, "checkpoint.pth.tar"),
            run_variables=to_restore,
            state_dict=model,
            optimizer=optimizer
        )
    start_epoch = to_restore["epoch"]

    # build the queue
    queue = None
    queue_path = os.path.join(args.dump_path, "queue" + str(args.rank) + ".pth")
    if os.path.isfile(queue_path):
        queue = torch.load(queue_path)["queue"]
    # the queue needs to be divisible by the batch size
    args.queue_length -= args.queue_length % (args.batch_size * args.world_size)

    cudnn.benchmark = True

    for epoch in range(start_epoch, args.epochs):

        # train the network for one epoch
        logger.info("============ Starting epoch %i ... ============" % epoch)

        # set sampler
        train_loader.sampler.set_epoch(epoch)

        # optionally starts a queue
        if args.queue_length > 0 and epoch >= args.epoch_queue_starts and queue is None:
            queue = torch.zeros(
                len(args.crops_for_assign),
                args.queue_length // args.world_size,
                args.feat_dim,
            ).cuda()

        # train the network
        scores, queue = train(train_loader, model, optimizer, epoch, lr_schedule, queue)
        training_stats.update(scores)

        # save checkpoints
        if args.rank == 0:
            save_dict = {
                "epoch": epoch + 1,
                "state_dict": model.state_dict(),
                "optimizer": optimizer.state_dict(),
            }
            if args.use_fp16:
                save_dict["amp"] = apex.amp.state_dict()
            torch.save(
                save_dict,
                os.path.join(args.dump_path, "checkpoint.pth.tar"),
            )
            if epoch % args.checkpoint_freq == 0 or epoch == args.epochs - 1:
                shutil.copyfile(
                    os.path.join(args.dump_path, "checkpoint.pth.tar"),
                    os.path.join(args.dump_checkpoints, "ckp-" + str(epoch) + ".pth"),
                )
        if queue is not None:
            torch.save({"queue": queue}, queue_path)


def train(train_loader, model, optimizer, epoch, lr_schedule, queue):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    model.train()
    use_the_queue = False

    end = time.time()
    for it, inputs in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        # update learning rate
        iteration = epoch * len(train_loader) + it
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr_schedule[iteration]

        # normalize the prototypes
        with torch.no_grad():
            w = model.module.prototypes.weight.data.clone()
            w = nn.functional.normalize(w, dim=1, p=2)
            model.module.prototypes.weight.copy_(w)

        # ============ multi-res forward passes ... ============
        embedding, output = model(inputs)
        embedding = embedding.detach()
        bs = inputs[0].size(0)

        # ============ swav loss ... ============
        loss = 0
        for i, crop_id in enumerate(args.crops_for_assign):
            with torch.no_grad():
                out = output[bs * crop_id: bs * (crop_id + 1)].detach()

                # time to use the queue
                if queue is not None:
                    if use_the_queue or not torch.all(queue[i, -1, :] == 0):
                        use_the_queue = True
                        out = torch.cat((torch.mm(
                            queue[i],
                            model.module.prototypes.weight.t()
                        ), out))
                    # fill the queue
                    queue[i, bs:] = queue[i, :-bs].clone()
                    queue[i, :bs] = embedding[crop_id * bs: (crop_id + 1) * bs]

                # get assignments
                q = distributed_sinkhorn(out)[-bs:]

            # cluster assignment prediction
            subloss = 0
            for v in np.delete(np.arange(np.sum(args.nmb_crops)), crop_id):
                x = output[bs * v: bs * (v + 1)] / args.temperature
                subloss -= torch.mean(torch.sum(q * F.log_softmax(x, dim=1), dim=1))
            loss += subloss / (np.sum(args.nmb_crops) - 1)
        loss /= len(args.crops_for_assign)

        # ============ backward and optim step ... ============
        optimizer.zero_grad()
        if args.use_fp16:
            with apex.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        # cancel gradients for the prototypes
        if iteration < args.freeze_prototypes_niters:
            for name, p in model.named_parameters():
                if "prototypes" in name:
                    p.grad = None
        optimizer.step()

        # ============ misc ... ============
        losses.update(loss.item(), inputs[0].size(0))
        batch_time.update(time.time() - end)
        end = time.time()
        if "APEX" in os.environ:
            lr_logger = optimizer.optim.param_groups[0]["lr"]
        else:
            lr_logger = optimizer.param_groups[0]["lr"]
        if args.rank ==0 and it % 50 == 0:
            logger.info(
                "Epoch: [{0}][{1}]\t"
                "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                "Data {data_time.val:.3f} ({data_time.avg:.3f})\t"
                "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                "Lr: {lr:.4f}".format(
                    epoch,
                    it,
                    batch_time=batch_time,
                    data_time=data_time,
                    loss=losses,
                    lr=lr_logger
                )
            )
    return (epoch, losses.avg), queue


@torch.no_grad()
def distributed_sinkhorn(out):
    Q = torch.exp(out / args.epsilon).t() # Q is K-by-B for consistency with notations from our paper
    B = Q.shape[1] * args.world_size # number of samples to assign
    K = Q.shape[0] # how many prototypes

    # make the matrix sums to 1
    sum_Q = torch.sum(Q)
    dist.all_reduce(sum_Q)
    Q /= sum_Q

    for it in range(args.sinkhorn_iterations):
        # normalize each row: total weight per prototype must be 1/K
        sum_of_rows = torch.sum(Q, dim=1, keepdim=True)
        dist.all_reduce(sum_of_rows)
        Q /= sum_of_rows
        Q /= K

        # normalize each column: total weight per sample must be 1/B
        Q /= torch.sum(Q, dim=0, keepdim=True)
        Q /= B

    Q *= B # the colomns must sum to 1 so that Q is an assignment
    return Q.t()


if __name__ == "__main__":
    # os.environ['APEX'] = 'true'
    args = get_args(
        data_path='/home/mattsacco/slideflow/slideflow/swav/small-imagenet',
        epochs=2,
        base_lr=0.6,
        final_lr=.0006,
        warmup_epochs=0,
        batch_size=16,
        size_crops=[224, 96],
        nmb_crops=[2, 6],
        min_scale_crops=[0.14, 0.05],
        max_scale_crops=[1., 0.14],
        freeze_prototypes_niters=5005,
        queue_length=0,
        arch='resnet50'
    )
    run_swav(args)
