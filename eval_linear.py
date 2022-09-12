# Copyright (c) Facebook, Inc. and its affiliates.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import argparse
import json
from pathlib import Path
import numpy as np

import torch
from torch import nn
import torch.distributed as dist
import torch.backends.cudnn as cudnn
from torchvision import datasets
from torchvision import transforms as pth_transforms
from torchvision import models as torchvision_models
from torch.utils.data import Subset

import utils
import vision_transformer as vits


def eval_linear(args):
    utils.fix_random_seeds(args.seed)
    if args.is_neps_run:
        pass  # utils.init_distributed_mode(args) done in pretraining
    else:
        utils.init_distributed_mode(args, None)
    print("git:\n  {}\n".format(utils.get_sha()))
    print("\n".join("%s: %s" % (k, str(v)) for k, v in sorted(dict(vars(args)).items())))
    cudnn.benchmark = True

    # ============ building network ... ============
    # if the network is a Vision Transformer (i.e. vit_tiny, vit_small, vit_base)
    if args.arch in vits.__dict__.keys():
        model = vits.__dict__[args.arch](patch_size=args.patch_size, num_classes=0)
        embed_dim = model.embed_dim * (args.n_last_blocks + int(args.avgpool_patchtokens))
    # if the network is a XCiT
    elif "xcit" in args.arch:
        model = torch.hub.load('facebookresearch/xcit:main', args.arch, num_classes=0)
        embed_dim = model.embed_dim
    # otherwise, we check if the architecture is in torchvision models
    elif args.arch in torchvision_models.__dict__.keys():
        model = torchvision_models.__dict__[args.arch]()
        embed_dim = model.fc.weight.shape[1]
        model.fc = nn.Identity()
    else:
        print(f"Unknow architecture: {args.arch}")
        sys.exit(1)
    model.cuda()
    model.eval()
    # load weights to evaluate
    utils.load_pretrained_weights(model, args.pretrained_weights, args.checkpoint_key, args.arch, args.patch_size)
    print(f"Model {args.arch} built.")


    # ============ preparing data ... ============
    if args.dataset == "ImageNet":
        train_crop_size = 224
        val_crop_size = 256
        normalize = pth_transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        args.num_labels = 1000
    elif args.dataset == "CIFAR-10":
        train_crop_size = 32
        val_crop_size = int(32 * 8 / 7)  # TODO: check out!
        normalize = pth_transforms.Normalize(mean=[0.4914, 0.4822, 0.4465], std=[0.2023, 0.1994, 0.2010])
        args.num_labels = 10
    elif args.dataset == "CIFAR-100":
        train_crop_size = 32
        val_crop_size = int(32 * 8 / 7)  # TODO: check out!
        normalize = pth_transforms.Normalize(mean=[0.5071, 0.4865, 0.4409], std=[0.2673, 0.2564, 0.2762])
        args.num_labels = 100
    elif args.dataset == "flowers":
        train_crop_size = 224
        val_crop_size = 256
        normalize = pth_transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        args.num_labels = 102
    elif args.dataset == "cars":
        train_crop_size = 224
        val_crop_size = 256
        normalize = pth_transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        args.num_labels = 196
    elif args.dataset == "inaturalist":
        train_crop_size = 224
        val_crop_size = 256
        normalize = pth_transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        args.num_labels = 5089
    else:
        raise NotImplementedError(f"Dataset '{args.dataset}' not implemented yet!")

    linear_classifier = LinearClassifier(embed_dim, num_labels=args.num_labels)
    linear_classifier = linear_classifier.cuda()
    linear_classifier = nn.parallel.DistributedDataParallel(linear_classifier, device_ids=[args.gpu])

    train_transform = pth_transforms.Compose([
        pth_transforms.RandomResizedCrop(train_crop_size),
        pth_transforms.RandomHorizontalFlip(),
        pth_transforms.ToTensor(),
        normalize,
    ])

    val_transform = pth_transforms.Compose([
        pth_transforms.Resize(val_crop_size, interpolation=3),
        pth_transforms.CenterCrop(train_crop_size),
        pth_transforms.ToTensor(),
        normalize,
    ])

    dataset_train = utils.get_dataset(args=args, transform=train_transform, mode="train")

    if args.is_neps_run:
        if args.dataset == "ImageNet":
            # balanced valid dataset
            # assumption: training dataset in `total_trainset` and you take care of doing the right thing with the transforms (as they are shared with `total_trainset`).
            # It is easiest to load the training set twice with the right transforms and use one for training and one for validation.
            dataset_train_trans = dataset_train
            dataset_val_trans = utils.get_dataset(args=args, transform=val_transform, mode="train")
            dataset_val = Subset(dataset_val_trans, args.assert_valid_idx)
            dataset_train = Subset(dataset_train_trans, args.assert_train_idx)
            print("using balanced validation set")

            train_sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)
            valid_sampler = torch.utils.data.distributed.DistributedSampler(dataset_val)
        else:
            dataset_val = utils.get_dataset(args=args, transform=val_transform, mode="train")
            
            dataset_percentage_usage = 100
            valid_size = 0.1  # TODO: check out! For CIFAR-10 I would use 0.1. For balanced ImageNet 0.1 might also be fine?
            num_train = int(len(dataset_train) / 100 * dataset_percentage_usage)
            indices = list(range(num_train))
            split = int(np.floor(valid_size * num_train))
            
            np.random.shuffle(indices)
    
            if np.isclose(valid_size, 0.0):
                train_idx, valid_idx = indices, indices
            else:
                train_idx, valid_idx = indices[split:], indices[:split]
            
            assert valid_idx[:10] == args.assert_valid_idx
    
            train_sampler = torch.utils.data.distributed.DistributedSampler(train_idx)
            valid_sampler = torch.utils.data.distributed.DistributedSampler(valid_idx)
    
    else:
        dataset_val = utils.get_dataset(args=args, transform=val_transform, mode="val")
        sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)
    
    train_loader = torch.utils.data.DataLoader(
        dataset_train,
        sampler=train_sampler if args.is_neps_run else sampler,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    val_loader = torch.utils.data.DataLoader(
        dataset_val,
        sampler=valid_sampler if args.is_neps_run else None,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
    )

    if args.evaluate:
        utils.load_pretrained_linear_weights(linear_classifier, args.arch, args.patch_size)
        test_stats = validate_network(args, val_loader, model, linear_classifier, args.n_last_blocks, args.avgpool_patchtokens)
        print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
        return
    
    if args.is_neps_run:
        if args.dataset == "ImageNet":
            print(f"Data loaded with {len(args.assert_train_idx)} train and {len(args.assert_valid_idx)} val imgs.")
        else:
            print(f"Data loaded with {len(train_idx)} train and {len(valid_idx)} val imgs.")
    else:
        print(f"Data loaded with {len(dataset_train)} train and {len(dataset_val)} val imgs.")

    # set optimizer
    optimizer = torch.optim.SGD(
        linear_classifier.parameters(),
        args.lr * (args.batch_size_per_gpu * utils.get_world_size()) / 256., # linear scaling rule
        momentum=0.9,
        weight_decay=0, # we do not apply weight decay
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, args.epochs, eta_min=0)

    # Optionally resume from a checkpoint
    to_restore = {"epoch": 0, "best_acc": 0.}
    utils.restart_from_checkpoint(
        os.path.join(args.output_dir, "checkpoint.pth.tar"),
        run_variables=to_restore,
        state_dict=linear_classifier,
        optimizer=optimizer,
        scheduler=scheduler,
    )
    start_epoch = to_restore["epoch"]
    best_acc = to_restore["best_acc"]
    
    if hasattr(args, "epoch_fidelity") and int(args.epoch_fidelity) == 6:
        epoch_end_range = 25
    elif hasattr(args, "epoch_fidelity") and (args.epoch_fidelity) == 25:
        epoch_end_range = 50
    elif hasattr(args, "epoch_fidelity") and (args.epoch_fidelity) == 100:
        epoch_end_range = 100
    else:
        # is_neps_run == False
        epoch_end_range = args.epochs

    for epoch in range(start_epoch, epoch_end_range):
        train_loader.sampler.set_epoch(epoch)

        train_stats = train(args, model, linear_classifier, optimizer, train_loader, epoch, args.n_last_blocks, args.avgpool_patchtokens)
        scheduler.step()

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch}
        if epoch % args.val_freq == 0 or epoch == args.epochs - 1:
            test_stats = validate_network(args, val_loader, model, linear_classifier, args.n_last_blocks, args.avgpool_patchtokens)
            print(f"Accuracy at epoch {epoch} of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
            if args.do_early_stopping:
                print("Do early stopping")
                best_acc = max(best_acc, test_stats["acc1"])
            else:
                print("Do NO early stopping")
                best_acc = test_stats["acc1"]
            print(f'Max accuracy so far: {best_acc:.2f}%')
            log_stats = {**{k: v for k, v in log_stats.items()},
                         **{f'test_{k}': v for k, v in test_stats.items()}}
        if utils.is_main_process():
            with (Path(args.output_dir) / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")
            save_dict = {
                "epoch": epoch + 1,
                "state_dict": linear_classifier.state_dict(),
                "optimizer": optimizer.state_dict(),
                "scheduler": scheduler.state_dict(),
                "best_acc": best_acc,
            }
            torch.save(save_dict, os.path.join(args.output_dir, "checkpoint.pth.tar"))
    print("Training of the supervised linear classifier on frozen features completed.\n"
                "Top-1 test accuracy: {acc:.1f}".format(acc=best_acc))
    
    if args.is_neps_run:
        print("OUTPUT_DIR: ", args.output_dir)
        with open(str(args.output_dir) + "/current_val_metric.txt", "w+") as f:
            f.write(f"{best_acc}\n")

def train(args, model, linear_classifier, optimizer, loader, epoch, n, avgpool):
    linear_classifier.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    for (inp, target) in metric_logger.log_every(loader, 20, header):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        target = target.cuda(non_blocking=True)

        # forward
        with torch.no_grad():
            if "vit" in args.arch:
                intermediate_output = model.get_intermediate_layers(inp, n)
                output = torch.cat([x[:, 0] for x in intermediate_output], dim=-1)
                if avgpool:
                    output = torch.cat((output.unsqueeze(-1), torch.mean(intermediate_output[-1][:, 1:], dim=1).unsqueeze(-1)), dim=-1)
                    output = output.reshape(output.shape[0], -1)
            else:
                output = model(inp)
        output = linear_classifier(output)

        # compute cross entropy loss
        loss = nn.CrossEntropyLoss()(output, target)

        # compute the gradients
        optimizer.zero_grad()
        loss.backward()

        # step
        optimizer.step()

        # log 
        torch.cuda.synchronize()
        metric_logger.update(loss=loss.item())
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def validate_network(args, val_loader, model, linear_classifier, n, avgpool):
    linear_classifier.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    for inp, target in metric_logger.log_every(val_loader, 20, header):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        target = target.cuda(non_blocking=True)

        # forward
        with torch.no_grad():
            if "vit" in args.arch:
                intermediate_output = model.get_intermediate_layers(inp, n)
                output = torch.cat([x[:, 0] for x in intermediate_output], dim=-1)
                if avgpool:
                    output = torch.cat((output.unsqueeze(-1), torch.mean(intermediate_output[-1][:, 1:], dim=1).unsqueeze(-1)), dim=-1)
                    output = output.reshape(output.shape[0], -1)
            else:
                output = model(inp)
        output = linear_classifier(output)
        loss = nn.CrossEntropyLoss()(output, target)

        if linear_classifier.module.num_labels >= 5:
            acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        else:
            acc1, = utils.accuracy(output, target, topk=(1,))

        batch_size = inp.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        if linear_classifier.module.num_labels >= 5:
            metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
    if linear_classifier.module.num_labels >= 5:
        print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
          .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))
    else:
        print('* Acc@1 {top1.global_avg:.3f} loss {losses.global_avg:.3f}'
          .format(top1=metric_logger.acc1, losses=metric_logger.loss))
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


class LinearClassifier(nn.Module):
    """Linear layer to train on top of frozen features"""
    def __init__(self, dim, num_labels=1000):
        super(LinearClassifier, self).__init__()
        self.num_labels = num_labels
        self.linear = nn.Linear(dim, num_labels)
        self.linear.weight.data.normal_(mean=0.0, std=0.01)
        self.linear.bias.data.zero_()

    def forward(self, x):
        # flatten
        x = x.view(x.size(0), -1)

        # linear layer
        return self.linear(x)


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Evaluation with linear classification on ImageNet')
    parser.add_argument('--n_last_blocks', default=4, type=int, help="""Concatenate [CLS] tokens
        for the `n` last blocks. We use `n=4` when evaluating ViT-Small and `n=1` with ViT-Base.""")
    parser.add_argument('--avgpool_patchtokens', default=False, type=utils.bool_flag,
        help="""Whether ot not to concatenate the global average pooled features to the [CLS] token.
        We typically set this to False for ViT-Small and to True with ViT-Base.""")
    parser.add_argument('--arch', default='vit_small', type=str, help='Architecture')
    parser.add_argument('--patch_size', default=16, type=int, help='Patch resolution of the model.')
    parser.add_argument('--pretrained_weights', default='', type=str, help="Path to pretrained weights to evaluate.")
    parser.add_argument("--checkpoint_key", default="teacher", type=str, help='Key to use in the checkpoint (example: "teacher")')
    parser.add_argument('--epochs', default=100, type=int, help='Number of epochs of training.')
    parser.add_argument("--lr", default=0.001, type=float, help="""Learning rate at the beginning of
        training (highest LR used during training). The learning rate is linearly scaled
        with the batch size, and specified here for a reference batch size of 256.
        We recommend tweaking the LR depending on the checkpoint evaluated.""")
    parser.add_argument('--batch_size_per_gpu', default=128, type=int, help='Per-GPU batch-size')
    parser.add_argument("--dist_url", default="env://", type=str, help="""url used to set up
        distributed training; see https://pytorch.org/docs/stable/distributed.html""")
    parser.add_argument("--local_rank", default=0, type=int, help="Please ignore and do not set this argument.")
    parser.add_argument('--data_path', default='/data/datasets/ImageNet/imagenet-pytorch/', type=str)
    parser.add_argument('--num_workers', default=10, type=int, help='Number of data loading workers per GPU.')
    parser.add_argument('--val_freq', default=1, type=int, help="Epoch frequency for validation.")
    parser.add_argument('--output_dir', default=".", help='Path to save logs and checkpoints')
    parser.add_argument('--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
    parser.add_argument("--is_neps_run", action="store_true", help="Set this flag to run a NEPS experiment.")
    parser.add_argument("--do_early_stopping", action="store_true", help="Set this flag to take the best test performance - Default by the DINO implementation.")
    parser.add_argument("--world_size", default=8, type=int, help="actually not needed here -- just for avoiding unrecognized arguments error")
    parser.add_argument("--gpu", default=8, type=int, help="actually not needed here -- just for avoiding unrecognized arguments error")
    parser.add_argument('--config_file_path', help="actually not needed here -- just for avoiding unrecognized arguments error")
    parser.add_argument('--seed', default=0, type=int, help='Random seed.')
    parser.add_argument('--dataset', default='ImageNet', choices=['ImageNet', 'CIFAR-10', 'CIFAR-100', 'DermaMNIST', 'cars', 'flowers', 'inaturalist'],
                        help='Select the dataset on which you want to run the pre-training. Default is ImageNet')
    
    args = parser.parse_args()
    eval_linear(args)
