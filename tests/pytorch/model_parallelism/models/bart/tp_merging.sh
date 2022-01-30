# USAGE:   ``sh ./tp_merging.sh $NUM_GPUS``
# EXAMPLE: ``sh ./tp_merging.sh 4``

NUM_GPUS=$1

python -m torch.distributed.launch \
       --nproc_per_node="$NUM_GPUS" \
       ../../testcases/merging.py \
       --task=seq2seq-lm \
       --model=facebook/bart-base \
       --tensor_parallel_size="$NUM_GPUS"