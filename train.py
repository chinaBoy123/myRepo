"""Training script"""
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '1'
import time
from tkinter.messagebox import NO
import numpy as np
import torch
from transformers import BertTokenizer

from lib.modules import set_seeds
from lib.vocab import deserialize_vocab
from lib.datasets import image_caption_bigru,image_caption_bert
from lib.model import Model
from lib.evaluation import i2t, t2i, AverageMeter, LogCollector, encode_data, compute_sims

import logging
import tensorboard_logger as tb_logger

import lib.arguments as arguments

def main():
    # Hyper Parameters
    parser = arguments.get_argument_parser()
    opt = parser.parse_args()
    set_seeds(opt.seed)

    if not os.path.exists(opt.model_name):
        os.makedirs(opt.model_name)
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)
    tb_logger.configure(opt.logger_name, flush_secs=5)

    logger = logging.getLogger(__name__)
    logger.info(opt)

    # Load Vocabulary
    if opt.text_enc_type=="bigru":
        if 'coco' in opt.data_name:
            vocab_file = 'coco_precomp_vocab.json'
        else:
            vocab_file = 'f30k_precomp_vocab.json'
        vocab = deserialize_vocab(os.path.join(opt.vocab_path, vocab_file))
        vocab.add_word('<mask>')  # add the mask, for testing cloze
        logger.info('Add <mask> token into the vocab')
        opt.vocab_size = len(vocab)
        # word embedding
        if opt.wemb_type is not None:
            opt.word2idx = vocab.word2idx
        else:
            opt.word2idx = None
        # dataloader 
        train_loader, val_loader = image_caption_bigru.get_loaders(
            opt.data_path, opt.data_name, vocab, opt.batch_size, opt.workers, opt)

    elif opt.text_enc_type=="bert":
        opt.word2idx = None

        # Load Tokenizer and Vocabulary
        # tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        root = os.path.expanduser("/home/ubuntu/Students/zhoutao/data/FNE_datasets/google/bert-base-uncased")

        tokenizer = BertTokenizer.from_pretrained(pretrained_model_name_or_path=root)
        vocab = tokenizer.vocab
        opt.vocab_size = len(vocab)
        train_loader, val_loader = image_caption_bert.get_loaders(
            opt.data_path, opt.data_name, tokenizer, opt.batch_size, opt.workers, opt)
    else:
        raise ValueError("Unknown text_enc_type: {}".format(opt.text_enc_type))

    model = Model(opt)
    
    # checkpoint = torch.load('/home/ubuntu/Students/zhoutao/code_updated/GARE-Net/runs/runX/checkpoint/f30k_bert/model_best.pth')
    # model.load_state_dict(checkpoint['model'])

    lr_schedules = [opt.lr_update, ]

    # optionally resume from a checkpoint
    start_epoch = 0
    if opt.resume:
        if os.path.isfile(opt.resume):
            logger.info("=> loading checkpoint '{}'".format(opt.resume))
            checkpoint = torch.load(opt.resume)
            start_epoch = checkpoint['epoch']
            best_rsum = checkpoint['best_rsum']

            model.load_state_dict(checkpoint['model'])
            # Eiters is used to show logs as the continuation of another training
            model.Eiters = checkpoint['Eiters']
            logger.info("=> loaded checkpoint '{}' (epoch {}, best_rsum {})"
                        .format(opt.resume, start_epoch, best_rsum))
            # validate(opt, val_loader, model)
            if opt.reset_start_epoch:
                start_epoch = 0
        else:
            logger.info("=> no checkpoint found at '{}'".format(opt.resume))
    # validate(opt, val_loader, model)
    # Train the Model
    best_rsum = 0
    for epoch in range(start_epoch, opt.num_epochs):
        logger.info(opt.logger_name)
        logger.info(opt.model_name)

        adjust_learning_rate(opt, model.optimizer, epoch, lr_schedules)

        if epoch >= opt.vse_mean_warmup_epochs:
            opt.max_violation = True
            model.set_max_violation(opt.max_violation)

        # train for one epoch
        train(opt, train_loader, model, epoch, val_loader)

        # evaluate on validation set
        rsum = validate(opt, val_loader, model)

        # remember best R@ sum and save checkpoint
        is_best = rsum > best_rsum
        best_rsum = max(rsum, best_rsum)
        if not os.path.exists(opt.model_name):
            os.mkdir(opt.model_name)
        if is_best:logger.info("Best model saving at epoch %d"%epoch)
        filename = 'checkpoint%s.pth' % (epoch)
        save_checkpoint({
            'epoch': epoch + 1,
            'model': model.state_dict(),
            'best_rsum': best_rsum,
            'opt': opt,
            'Eiters': model.Eiters,
        }, is_best, filename=filename, prefix=opt.model_name + '/')


def train(opt, train_loader, model, epoch, val_loader):
    # average meters to record the training statistics
    logger = logging.getLogger(__name__)
    batch_time = AverageMeter()
    data_time = AverageMeter()
    train_logger = LogCollector()

    if epoch<1:
        logger.info('image encoder trainable parameters: {}'.format(count_params(model.img_enc)))
        logger.info('txt encoder trainable parameters: {}'.format(count_params(model.txt_enc)))
        logger.info('similarity encoder trainable parameters: {}'.format(count_params(model.sim_enc)))

    num_loader_iter = len(train_loader.dataset) // train_loader.batch_size + 1

    end = time.time()
    # opt.viz = True
    for i, train_data in enumerate(train_loader):
        # switch to train mode
        model.train_start()

        # measure data loading time
        data_time.update(time.time() - end)

        # make sure train logger is used
        model.logger = train_logger

        # Update the model
        images, img_lengths, captions, lengths, boxes, _ = train_data
        # print(f"images[0]:{images[0]}\nimages[4]:{images[4]}\nimages[5]:{images[5]}") 
        # images: (128, 36, 2048)
        # img_lengths: (128)
        # captions: (128, 30)
        # lengths: (128)
        model.train_emb(images, captions, lengths, image_lengths=img_lengths, boxes=boxes) # 核心代码

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # logger.info log info
        if model.Eiters % opt.log_step == 0:
            logging.info(
                'Epoch: [{0}][{1}/{2}]\t'
                '{e_log}\t'
                'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                    .format(
                    epoch, i, len(train_loader.dataset) // train_loader.batch_size + 1, batch_time=batch_time,
                    data_time=data_time, e_log=str(model.logger)))

        # Record logs in tensorboard
        tb_logger.log_value('epoch', epoch, step=model.Eiters)
        tb_logger.log_value('step', i, step=model.Eiters)
        tb_logger.log_value('batch_time', batch_time.val, step=model.Eiters)
        tb_logger.log_value('data_time', data_time.val, step=model.Eiters)
        model.logger.tb_log(tb_logger, step=model.Eiters)


def validate(opt, val_loader, model):
    
    logger = logging.getLogger(__name__)
    model.val_start()
    with torch.no_grad():
        # compute the encoding for all the validation images and captions
        img_embs, cap_embs, img_lens, cap_lens = encode_data(model, val_loader, opt.log_step, logging.info)

    img_embs = np.array([img_embs[i] for i in range(0, len(img_embs), 5)])

    start = time.time()
    # sims = compute_sim(img_embs, cap_embs)
    with torch.no_grad():
        sims = compute_sims(img_embs, cap_embs, img_lens, cap_lens, model)
    end = time.time()
    logger.info("calculate similarity time:".format(end - start))

    # caption retrieval
    npts = img_embs.shape[0]
    # (r1, r5, r10, medr, meanr) = i2t(img_embs, cap_embs, cap_lens, sims)
    (r1, r5, r10, medr, meanr) = i2t(npts, sims)
    logging.info("Image to text: %.1f, %.1f, %.1f, %.1f, %.1f" %
                 (r1, r5, r10, medr, meanr))
    # image retrieval
    # (r1i, r5i, r10i, medri, meanr) = t2i(img_embs, cap_embs, cap_lens, sims)
    (r1i, r5i, r10i, medri, meanr) = t2i(npts, sims)
    logging.info("Text to image: %.1f, %.1f, %.1f, %.1f, %.1f" %
                 (r1i, r5i, r10i, medri, meanr))
    # sum of recalls to be used for early stopping
    currscore = r1 + r5 + r10 + r1i + r5i + r10i
    logger.info('Current rsum is {}'.format(currscore))

    # record metrics in tensorboard
    tb_logger.log_value('r1', r1, step=model.Eiters)
    tb_logger.log_value('r5', r5, step=model.Eiters)
    tb_logger.log_value('r10', r10, step=model.Eiters)
    tb_logger.log_value('medr', medr, step=model.Eiters)
    tb_logger.log_value('meanr', meanr, step=model.Eiters)
    tb_logger.log_value('r1i', r1i, step=model.Eiters)
    tb_logger.log_value('r5i', r5i, step=model.Eiters)
    tb_logger.log_value('r10i', r10i, step=model.Eiters)
    tb_logger.log_value('medri', medri, step=model.Eiters)
    tb_logger.log_value('meanr', meanr, step=model.Eiters)
    tb_logger.log_value('rsum', currscore, step=model.Eiters)

    return currscore

def save_checkpoint(state, is_best, filename='checkpoint.pth', prefix=''):
    logger = logging.getLogger(__name__)
    tries = 15

    # deal with unstable I/O. Usually not necessary.
    while tries:
        try:
            # torch.save(state, prefix + filename)
            if is_best:
                torch.save(state, prefix + 'model_best.pth')
        except IOError as e:
            error = e
            tries -= 1
        else:
            break
        logger.info('model save {} failed, remaining {} trials'.format(filename, tries))
        if not tries:
            raise error


def adjust_learning_rate(opt, optimizer, epoch, lr_schedules):
    logger = logging.getLogger(__name__)
    """Sets the learning rate to the initial LR
       decayed by 10 every opt.lr_update epochs"""
    if epoch in lr_schedules:
        logger.info('Current epoch num is {}, decrease all lr by 10'.format(epoch, ))
        for param_group in optimizer.param_groups:
            old_lr = param_group['lr']
            new_lr = old_lr * 0.1
            param_group['lr'] = new_lr
            logger.info('new lr {}'.format(new_lr))


def count_params(model):
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    return params


if __name__ == '__main__':
    main()
