#!/usr/bin/env python3
import argparse
import logging
import os
import numpy as np

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

from config import cfg
from data import get_loader
from memory_buffer import Buffer
from model import Model, get_model
from utils.logger import setup_logger
from utils.loss import BCEauto
from utils.utils import AverageMeter, save_config, set_seed
from utils.metrics import Metrics
from utils.basic import get_counts, get_counts_labels

device = 'cuda:1' if torch.cuda.is_available() else 'cpu'


def train(cfg, model, train_loader, tid, mem, logger, writer, metrics, num_iter=1):
    model.train()
    criterion =  torch.nn.CrossEntropyLoss()
    avg_loss = AverageMeter()
    batch_size = cfg.SOLVER.BATCH_SIZE
    num_batches = cfg.DATA.TRAIN.NUM_SAMPLES // batch_size
    optimizer = optim.SGD(model.parameters(), lr = cfg.OPTIMIZER.LR)
    acc = 0.0
    for epoch_idx in range(0, cfg.SOLVER.NUM_EPOCHS):
        for batch_idx, data in enumerate(train_loader):
            for _ in range(num_iter):
                writer_idx = batch_idx * batch_size + (epoch_idx * num_batches * batch_size)
                x, y = data
                x_orig, y_orig = x, y
                x = x.view(min(x.shape[0], cfg.SOLVER.BATCH_SIZE), -1)
                sampled_mem, _ = mem.sample()
                if sampled_mem is not None:
                    x_c = torch.stack([x[0] for x in sampled_mem])
                    y_c = torch.stack([x[1] for x in sampled_mem])
                    x, y = torch.cat((x, x_c)), torch.cat((y, y_c))
                x = x.to(device)
                y = y.to(device)
                output = model(x)
                loss = criterion(output, y)
                # if torch.isnan(loss):
                #     import ipdb; ipdb.set_trace()
                pred = output.argmax(dim=1, keepdim=True)
                acc += pred.eq(y.view_as(pred)).sum().item() / x.shape[0]
                avg_loss.update(loss)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            mem.fill(x_orig, y_orig, tid=tid)
            writer.add_scalar(f'loss-{tid}', loss, writer_idx)
            mem.num_seen += batch_size
            if batch_idx % cfg.SYSTEM.LOG_FREQ==0:
                logger.debug(f'Batch Id:{batch_idx}, Loss:{loss}, Average Loss:{avg_loss.avg}')
                print(f'Labels Y : {get_counts_labels(y)},\
                    Memory: {get_counts(mem.memory)},\
                    Eff Size: {mem.eff_size},\
                    Memory Size: {len(mem.memory)},\
                    Num Seen:{mem.num_seen}')
        logger.info(f'Task Id:{tid}, Acc:{acc/len(train_loader)}')
    test(cfg, model, logger, writer, metrics, tid)


def test(cfg, model, logger, writer, metrics, tid_done):
    model.eval()
    criterion = torch.nn.CrossEntropyLoss()
    test_loaders = [(tid, get_loader(cfg, False, tid)) for tid in range(tid_done+1)]
    avg_meter = AverageMeter()
    for tid, test_loader in test_loaders:
        avg_meter.reset()
        for idx, data in enumerate(test_loader):
            x, y = data
            x = x.to(device)
            y = y.to(device)
            output = model(x)
            test_loss = criterion(output, y)
            pred = output.argmax(dim=1, keepdim=True)
            acc = metrics.accuracy(tid, tid_done, pred, y)
        metrics.avg_accuracy(tid, tid_done, len(test_loader.dataset))
        metrics.forgetting(tid, tid_done)
    logger.info(f'Task Done:{tid_done},\
                  Test Acc:{metrics.acc_task(tid_done)},\
                  Test Forgetting:{metrics.forgetting_task(tid_done)}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--clean_run', type=bool, default=True)
    parser.add_argument('--config_file', type=str, default="")
    parser.add_argument("opts", default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()

    set_seed(cfg)
    log_dir, chkpt_dir = save_config(cfg.SYSTEM.SAVE_DIR, cfg, args.clean_run)
    logger = setup_logger(cfg.SYSTEM.EXP_NAME, os.path.join(cfg.SYSTEM.SAVE_DIR, cfg.SYSTEM.EXP_NAME), 0)
    writer = SummaryWriter(log_dir)
    metrics = Metrics(cfg.SOLVER.NUM_TASKS)
    if cfg.DATA.TYPE == 'mnist':
        model = get_model('mlp', input_size = cfg.MODEL.MLP.INPUT_SIZE, hidden_size = cfg.MODEL.MLP.HIDDEN_SIZE, out_size = cfg.MODEL.MLP.OUTPUT_SIZE)
    elif cfg.DATA.TYPE == 'cifar':
        model = get_model('resnet', n_cls = cfg.DATA.NUM_CLASSES)
    model.to(device)
    mem = Buffer(cfg)
    for tid in range(cfg.SOLVER.NUM_TASKS):
        train_loader = get_loader(cfg, True, tid)
        train(cfg, model, train_loader, tid, mem, logger, writer, metrics)
    logger.info(f'Avg Acc:{metrics.acc_task(cfg.SOLVER.NUM_TASKS-1)},\
                  Avg Forgetting:{metrics.forgetting_task(cfg.SOLVER.NUM_TASKS-1)}')
