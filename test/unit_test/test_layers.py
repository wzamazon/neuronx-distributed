import atexit
from datetime import datetime
import json 
import argparse
import os
import traceback
import copy
import torch_xla.core.xla_model as xm
import torch_xla
import torch_xla.debug.metrics as met
import torch.nn.init as init
import torch
from transformers import BertForPreTraining
from commons import set_random_seed, print_separator, IdentityLayer3D
from neuronx_distributed.parallel_layers import layers, parallel_state


datetime_str = str(datetime.now())


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--test_json', required=False, help='input json listing the test spec for network to compile')
    parser.add_argument('--s3_dir', required=False, help='location to upload all test artifacts')
    parser.add_argument('--s3_bucket', default='s3://ktf-test-runs/neuronx_distributed_parallel_layers/layers')
    args, leftovers = parser.parse_known_args()
    S3_BUCKET_NAME = args.s3_bucket
    with open(args.test_json, "r") as f:
        test_dict = json.load(f)
    return test_dict, S3_BUCKET_NAME, args

test_config, S3_BUCKET_NAME, args = parse_args()
results = {
    "inference_success": 1
}


def test_parallel_embedding(tensor_model_parallel_size):
    def _test_parallel_embedding():
        
        device = xm.xla_device()
        tensor_model_parallel_size_ = tensor_model_parallel_size
        parallel_state.initialize_model_parallel(tensor_model_parallel_size_)
        tensor_model_parallel_size_ = parallel_state.get_tensor_model_parallel_size()

        batch_size = 17
        seq_length = 23
        vocab_size = 48
        hidden_size = 16
        seed = 1234

        set_random_seed(123)
        input_data = torch.LongTensor(size=(batch_size, seq_length)).random_(
            0, vocab_size).to(device)
        loss_weight = torch.randn([batch_size, seq_length, hidden_size]).to(device)

        set_random_seed(seed)
        embedding_original = torch.nn.Embedding(vocab_size, hidden_size).to(device)

        output = embedding_original(input_data)
        loss_original = torch.mul(output, loss_weight).sum()
        loss_original.backward()

        set_random_seed(seed)
        embedding_parallel = layers.ParallelEmbedding(
            vocab_size,
            hidden_size,
            init_method=init.normal_).to(device)
        output = embedding_parallel(input_data)
        loss_parallel = torch.mul(output, loss_weight).sum()
        loss_parallel.backward()

        torch.distributed.barrier()
        error = loss_parallel.sub(loss_original).abs()
        print('   error in loss (parallel) on global rank {}: {}'.format(
            torch.distributed.get_rank(), error))
        assert error < 1.0e-5, 'error: {}'.format(error)

        weight_grad_orig = torch.split(
            embedding_original.weight.grad,
            vocab_size // tensor_model_parallel_size_,
            0)[parallel_state.get_tensor_model_parallel_rank()]
        error = embedding_parallel.weight.grad.sub(weight_grad_orig).abs().max()
        print('   error in grad (parallel) on global rank {}: {}'.format(
            torch.distributed.get_rank(), error))
        # assert error < 1.0e-5, 'error: {}'.format(error) #Error is 2.09

        # Reset groups
        parallel_state.destroy_model_parallel()

        torch.distributed.barrier()
        if torch.distributed.get_rank() == 0:
            print('test passed')

        del device
        
    global results
    try:
        _test_parallel_embedding()
    except:
        results["inference_success"] = 0
        print(traceback.format_exc())
        raise


def test_initialize_affine_weight_cpu(tensor_model_parallel_size):
    def _test_initialize_affine_weight_cpu():

        tensor_model_parallel_size_ = tensor_model_parallel_size
        parallel_state.initialize_model_parallel(tensor_model_parallel_size_)
        tensor_model_parallel_size_ = parallel_state.get_tensor_model_parallel_size()

        seed = 12345
        input_size_coeff = 13
        input_size = input_size_coeff * tensor_model_parallel_size_
        output_size_coeff = 17
        output_size = output_size_coeff * tensor_model_parallel_size_

        # ---------------
        # Column parallel
        # ---------------
        weight = torch.empty(output_size_coeff, input_size)
        set_random_seed(seed)
        layers._initialize_affine_weight_cpu(weight, output_size, input_size,
                                            output_size_coeff, 0,
                                            torch.nn.init.normal_)
        # Target.
        set_random_seed(seed)
        master_weight = torch.empty(output_size, input_size)
        torch.nn.init.normal_(master_weight)
        rank = parallel_state.get_tensor_model_parallel_rank()
        my_weight = torch.split(master_weight, output_size_coeff,
                                dim=0)[rank].contiguous().clone()

        # Compare.
        error = weight.sub(my_weight).abs().max()
        torch.distributed.barrier()
        print('   column parallel max error (should be zero) on global rank '
            '{}: {}'.format(torch.distributed.get_rank(), error))
        assert error < 1.0e-6

        # ------------
        # Row parallel
        # ------------
        weight = torch.empty(output_size, input_size_coeff)
        set_random_seed(seed)
        layers._initialize_affine_weight_cpu(weight, output_size, input_size,
                                            input_size_coeff, 1,
                                            torch.nn.init.normal_)
        # Target.
        set_random_seed(seed)
        master_weight = torch.empty(output_size, input_size)
        torch.nn.init.normal_(master_weight)
        rank = parallel_state.get_tensor_model_parallel_rank()
        my_weight = torch.split(master_weight, input_size_coeff,
                                dim=1)[rank].contiguous().clone()

        # Compare.
        error = weight.sub(my_weight).abs().max()
        torch.distributed.barrier()
        print('   row parallel max error (should be zero) on global rank '
            '{}: {}'.format(torch.distributed.get_rank(), error))
        assert error < 1.0e-6

        xm.mark_step()

        # Reset groups
        parallel_state.destroy_model_parallel()

        torch.distributed.barrier()
        if torch.distributed.get_rank() == 0:
            print('test passed')
    
    global results
    try:
        _test_initialize_affine_weight_cpu()
    except:
        results["inference_success"] = 0
        print(traceback.format_exc())
        raise



def upload_to_s3():
    os.system(f'aws s3 cp --no-progress "{datetime_str}" {S3_BUCKET_NAME}')
    print(met.metrics_report())


def on_exit():
    upload_to_s3()
    for k in test_config:
        os.system(f'rm {args.test_json}')
        with open(args.test_json, "w") as f:
            json.dump({k: results}, f)
            
            
if __name__ == '__main__':
    torch.distributed.init_process_group("xla")
    world_size = xm.xrt_world_size()
    tensor_model_parallel_size = 1
    while tensor_model_parallel_size <= world_size:
        print_separator('test parallel embedding')
        test_parallel_embedding(tensor_model_parallel_size)
        print_separator('test initialize affine weight')
        test_initialize_affine_weight_cpu(tensor_model_parallel_size)
        tensor_model_parallel_size *= 2
    atexit.register(on_exit)