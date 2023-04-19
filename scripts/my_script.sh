python -m torch.distributed.launch --nproc_per_node=4 main_swav.py --data_path /home/mattsacco/slideflow/slideflow/swav/small-imagenet --epochs 2 --base_lr 0.6 --final_lr 0.0006 --warmup_epochs 0 --batch_size 16 --size_crops 224 96 --nmb_crops 2 6 --min_scale_crops 0.14 0.05 --max_scale_crops 1. 0.14 --freeze_prototypes_niters 5005 --queue_length 0 

python -m torch.distributed.launch --nproc_per_node=1 main_swav.py --data_path /home/mattsacco/slideflow/slideflow/swav/small-imagenet --epochs 2 --base_lr 0.6 --final_lr 0.0006 --warmup_epochs 0 --batch_size 16 --size_crops 224 96 --nmb_crops 2 6 --min_scale_crops 0.14 0.05 --max_scale_crops 1. 0.14 --freeze_prototypes_niters 5005 --queue_length 0 

