import torch
import torch.distributed as dist

from oslo.torch.distributed import ParallelContext, ParallelMode
from oslo.torch.nn import VocabParallelEmbedding2D

parallel_context = ParallelContext.from_torch(
    data_parallel_size=1,
    pipeline_parallel_size=1,
    tensor_parallel_size=4,
    tensor_parallel_mode="2d",
)

torch.set_printoptions(sci_mode=False)
torch.manual_seed(0)
summa_dim = parallel_context.get_world_size(ParallelMode.TENSOR_2D_COL)
input_ = torch.LongTensor([[1, 2, 3, 4], [5, 6, 7, 8]]).cuda()
dist.broadcast(input_, src=0)

vocab_embedding = torch.nn.Embedding(10, 8).cuda()
if parallel_context.get_global_rank() == 0:
    out = vocab_embedding(input_)
    print(f"original output: \n{out}\n")

dist.barrier()

# split weight into 0:[0, 0], 1:[1, 0], 2:[0, 1], 3:[1, 1]
w = vocab_embedding.weight.data.chunk(2, dim=1)[
    parallel_context.get_local_rank(ParallelMode.TENSOR_2D_ROW)
]
w = w.chunk(2, dim=0)[parallel_context.get_local_rank(ParallelMode.TENSOR_2D_COL)]

vocab_embedding_2d = VocabParallelEmbedding2D(10, 8, parallel_context)
vocab_embedding_2d.weight.data = w

out = vocab_embedding_2d(input_)
out_list = [torch.zeros_like(out) for _ in range(summa_dim)]
dist.all_gather(out_list, out, parallel_context.get_group(ParallelMode.TENSOR_2D_ROW))
out = torch.cat(out_list, dim=-1)
out_list = [torch.zeros_like(out) for _ in range(summa_dim)]
dist.all_gather(out_list, out, parallel_context.get_group(ParallelMode.TENSOR_2D_COL))
out = torch.cat(out_list, dim=0)

if parallel_context.get_global_rank() == 0:
    print(f"parallel output: \n{out}\n")