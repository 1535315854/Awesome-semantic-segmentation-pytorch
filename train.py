import argparse
import time
import os

import torch
import torch.utils.data as data
import torch.backends.cudnn as cudnn

from torchvision import transforms
from data_loader import get_segmentation_dataset
from models.model_zoo import get_segmentation_model
from utils.parallel import DataParallelModel, DataParallelCriterion
from utils.lr_scheduler import LRScheduler
from utils.score import SegmentationMetric
from utils.loss import MixSoftmaxCrossEntropyLoss


def parse_args():
    parser = argparse.ArgumentParser(description='Semantic Segmentation Training With Pytorch')
    # model and dataset
    parser.add_argument('--model', type=str, default='fcn32s',
                        choices=['fcn32s/fcn16s/fcn8s/psp/deeplabv3/danet/denseaspp/bisenet'],
                        help='model name (default: fcn32s)')
    parser.add_argument('--backbone', type=str, default='vgg16',
                        choices=['vgg16/resnet18/resnet50/resnet101/resnet152/densenet121/161/169/201'],
                        help='backbone name (default: vgg16)')
    parser.add_argument('--dataset', type=str, default='pascal_voc',
                        choices=['pascal_voc/pascal_aug/ade20k/citys'],
                        help='dataset name (default: pascal_voc)')
    parser.add_argument('--base-size', type=int, default=520,
                        help='base image size')
    parser.add_argument('--crop-size', type=int, default=480,
                        help='crop image size')
    parser.add_argument('--train-split', type=str, default='train',
                        help='dataset train split (default: train)')
    # training hyper params
    parser.add_argument('--aux', action='store_true', default=False,
                        help='Auxiliary loss')
    parser.add_argument('--aux-weight', type=float, default=0.5,
                        help='auxiliary loss weight')
    parser.add_argument('--epochs', type=int, default=60, metavar='N',
                        help='number of epochs to train (default: 60)')
    parser.add_argument('--start_epoch', type=int, default=0,
                        metavar='N', help='start epochs (default:0)')
    parser.add_argument('--batch-size', type=int, default=4, metavar='N',
                        help='input batch size for training (default: 4)')
    parser.add_argument('--lr', type=float, default=1e-2, metavar='LR',
                        help='learning rate (default: 1e-4)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='momentum (default: 0.9)')
    parser.add_argument('--weight-decay', type=float, default=1e-4, metavar='M',
                        help='w-decay (default: 5e-4)')
    # checking point
    parser.add_argument('--resume', type=str, default=None,
                        help='put the path to resuming file if needed')
    parser.add_argument('--save-folder', default='~/.torch/models',
                        help='Directory for saving checkpoint models')
    # evaluation only
    parser.add_argument('--eval', action='store_true', default=False,
                        help='evaluation only')
    parser.add_argument('--no-val', action='store_true', default=True,
                        help='skip validation during training')
    args = parser.parse_args()

    # default settings for epochs, batch_size and lr
    if args.epochs is None:
        epoches = {
            'coco': 30,
            'pascal_aug': 80,
            'pascal_voc': 50,
            'pcontext': 80,
            'ade20k': 180,
            'citys': 240,
        }
        args.epochs = epoches[args.dataset.lower()]
    if args.lr is None:
        lrs = {
            'coco': 0.004,
            'pascal_aug': 0.001,
            'pascal_voc': 0.0001,
            'pcontext': 0.001,
            'ade20k': 0.004,
            'citys': 0.004,
        }
        args.lr = lrs[args.dataset.lower()] / 16 * args.batch_size

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    cudnn.benchmark = True
    args.device = device
    print(args)
    return args


class Trainer(object):
    def __init__(self, args):
        self.args = args

        # image transform
        input_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize([.485, .456, .406], [.229, .224, .225]),
        ])
        # dataset and dataloader
        data_kwargs = {'transform': input_transform, 'base_size': args.base_size, 'crop_size': args.crop_size}
        train_dataset = get_segmentation_dataset(args.dataset, split=args.train_split, mode='train', **data_kwargs)
        val_dataset = get_segmentation_dataset(args.dataset, split='val', mode='val', **data_kwargs)

        self.train_loader = data.DataLoader(dataset=train_dataset,
                                            batch_size=args.batch_size,
                                            shuffle=True)

        self.val_loader = data.DataLoader(dataset=val_dataset,
                                          batch_size=1,
                                          shuffle=False)

        # create network
        self.model = get_segmentation_model(model=args.model, dataset=args.dataset, backbone=args.backbone,
                                            aux=args.aux, crop_size=args.crop_size)

        # create criterion
        self.criterion = MixSoftmaxCrossEntropyLoss(args.aux, args.aux_weight, ignore_label=-1)

        # for multi-GPU
        if torch.cuda.is_available():
            self.model = DataParallelModel(self.model).cuda()
            self.criterion = DataParallelCriterion(self.criterion).cuda()

        # resume checkpoint if needed
        if args.resume:
            if os.path.isfile(args.resume):
                name, ext = os.path.splitext(args.resume)
                assert ext == '.pkl' or '.pth', 'Sorry only .pth and .pkl files supported.'
                print('Resuming training, loading {}...'.format(args.resume))
                self.model.load_state_dict(torch.load(args.resume, map_location=lambda storage, loc: storage))

        # optimizer
        self.optimizer = torch.optim.SGD(self.model.parameters(),
                                         lr=args.lr,
                                         momentum=args.momentum,
                                         weight_decay=args.weight_decay)

        # lr scheduling
        self.lr_scheduler = LRScheduler(mode='poly', base_lr=args.lr, nepochs=args.epochs,
                                        iters_per_epoch=len(self.train_loader), power=0.9)

        # evaluation metrics
        self.metric = SegmentationMetric(train_dataset.num_class)

    def train(self):
        self.model.train()
        cur_iters = 0
        start_time = time.time()
        for epoch in range(self.args.start_epoch, self.args.epochs):
            for i, (images, targets) in enumerate(self.train_loader):
                cur_lr = self.lr_scheduler(cur_iters)
                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = cur_lr

                images = images.to(self.args.device)
                targets = targets.to(self.args.device)

                outputs = self.model(images)
                loss = self.criterion(outputs, targets)

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                cur_iters += 1
                if cur_iters % 10 == 0:
                    print('Epoch: [%2d/%2d] Iter [%4d/%4d] || Time: %4.4f sec || lr: %.8f || Loss: %.4f' % (
                        epoch, self.args.epochs, i + 1, len(self.train_loader),
                        time.time() - start_time, cur_lr, loss.item()))

            if not self.args.no_val:
                self.validation(epoch)

            # save every 10 epoch
            if epoch != 0 and epoch % 10 == 0:
                print('Saving state, epoch:', epoch)
                self.save_checkpoint()
        self.save_checkpoint()

    def validation(self, epoch):
        # total_inter, total_union, total_correct, total_label = 0, 0, 0, 0
        self.metric.reset()
        self.model.eval()
        for i, (image, target) in enumerate(self.val_loader):
            image = image.to(self.args.device)

            outputs = self.model(image)
            pred = torch.argmax(outputs[0], 1)
            pred = pred.cpu().data.numpy()
            self.metric.update(pred, target.numpy())
            pixAcc, mIoU = self.metric.get()
            print('Epoch %d, Sample %d, validation pixAcc: %.3f, mIoU: %.3f' % (epoch, i + 1, pixAcc, mIoU))

    def save_checkpoint(self):
        """Save Checkpoint"""
        directory = os.path.expanduser(self.args.save_folder)
        if not os.path.exists(directory):
            os.makedirs(directory)
        filename = '{}_{}_{}.pth'.format(self.args.model, self.args.backbone, self.args.dataset)
        save_path = os.path.join(directory, filename)
        torch.save(self.model.state_dict(), save_path)


if __name__ == '__main__':
    args = parse_args()
    trainer = Trainer(args)
    if args.eval:
        print('Evaluation model: ', args.resume)
        trainer.validation(args.start_epoch)
    else:
        print('Starting Epoch: %d, Total Epochs: %d' % (args.start_epoch, args.epochs))
        trainer.train()
