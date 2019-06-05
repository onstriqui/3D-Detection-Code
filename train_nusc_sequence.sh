#~/bin/bash
python -m torch.distributed.launch --nproc_per_node=8 --master_addr="127.0.0.1" --master_port=8667 ./tools/train_sequence.py --cfg configs/nuscenes/all_nuscene_sequence_fhd.yaml --model_dir nusc_sequence
