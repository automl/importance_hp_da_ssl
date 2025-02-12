#!/bin/bash
#SBATCH -p alldlc_gpu-rtx2080
##SBATCH -q dlc-wagnerd
#SBATCH --gres=gpu:1
#SBATCH -J DA_C10_NEPS_DINO
##SBATCH -t 23:59:59
#SBATCH --array 0-49%10

pip list

source activate dino

port=`python cluster/find_free_port.py`
echo "found free port $port"

mkdir -p /tmp/dino_communication
filename=/tmp/dino_communication/$(openssl rand -hex 12)

python -m torch.distributed.launch --master_port=$port --nproc_per_node=1 main_dino.py --config_file_path $filename --arch vit_small --output_dir /work/dlclarge2/wagnerd-metassl-experiments/dino/CIFAR-10/$EXPERIMENT_NAME --batch_size_per_gpu 256 --saveckp_freq 100 --gpu 1 --world_size 1 --dataset CIFAR-10 --epochs 800 --seed $SEED --is_neps_run

