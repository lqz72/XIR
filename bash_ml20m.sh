#!/bin/bash
export CUDA_VISIBLE_DEVICES="1"
# bz 2048 lr 0.001 wd 1e-6

logs='log_ml20m/test/sample'
data=ml-20m
for b in 2048
do
    for lr in 0.001
    do
        for w in  0.000001
        do
            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 1 --batch_size $b --data_name ${data} --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 1 --batch_size $b --data_name ${data} --sample_from_batch --epoch 50;
            
            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 2 --batch_size $b --data_name ${data} --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 2 --batch_size $b --data_name ${data} --sample_from_batch --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 4 --batch_size $b --data_name ${data} --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 4 --batch_size $b --data_name ${data} --sample_from_batch --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 5 --batch_size $b --data_name ${data} --epoch 50;

            # python run1.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 5 --batch_size $b --data_name ${data} --sample_from_batch --epoch 50;

            python run2.py --device cuda --learning_rate ${lr} --weight_decay ${w} --log_path ${logs} --debias 6 --batch_size $b --data_name ${data} --epoch 50 --sample_size ${b};

        done
    done
done