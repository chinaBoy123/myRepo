"""COCO dataset loader"""
import torch
import torch.utils.data as data
import os
import os.path as osp
import numpy as np
from imageio import imread
import random
import json
import cv2
import nltk

import logging

logger = logging.getLogger(__name__)

class PrecompRegionDataset(data.Dataset):
    """
    Load precomputed captions and image features for COCO or Flickr
    """

    def __init__(self, data_path, data_name, data_split, vocab, opt, train):
        self.vocab = vocab
        self.opt = opt
        self.train = train
        self.data_path = data_path
        self.data_name = data_name
        self.data_split = data_split

        loc_cap = os.path.join(data_path, data_name)
        loc_image = os.path.join(data_path, data_name)
        loc_boxes = os.path.join(data_path, data_name)
        # loc_bbox = os.path.join(data_path, data_name)
        # loc_tags = os.path.join(data_path, data_name)
        # loc_sizes = os.path.join(data_path, data_name)
        
        # Captions
        self.captions = []
        # self.tags = []
        with open(osp.join(loc_cap, '%s_caps.txt' % data_split), 'r') as f:
            for line in f:
                self.captions.append(line.strip())
        # with open(osp.join(loc_tags, '%s_tags.txt' % data_split), 'r') as f:
        #     for line in f:
        #         self.tags.append(line.strip())
        # Image features
        self.images = np.load(os.path.join(loc_image, '%s_ims.npy' % data_split))
        self.boxes = np.load(os.path.join(loc_boxes, '%s_boxes.npy' % data_split))
        # self.bbox = np.load(os.path.join(loc_bbox, '%s_ims_bbx.npy' % data_split))
        # self.sizes = np.load(os.path.join(loc_sizes, '%s_ims_size.npy' % data_split), allow_pickle=True)
        
        self.length = len(self.captions)
        # rkiros data has redundancy in images, we divide by 5, 10crop doesn't
        num_images = len(self.images)

        if num_images != self.length:
            self.im_div = 5
        else:
            self.im_div = 1
        # the development set for coco is large and so validation would be slow
        if data_split == 'dev':
            self.length = 5000

        if hasattr(opt,"obj_drop_rate"):
            self.obj_drop_rate = opt.obj_drop_rate
        else:
            self.obj_drop_rate = 0.2

    def __getitem__(self, index):
        # handle the image redundancy
        img_index = index // self.im_div
        caption = self.captions[index]
        # tag = self.tags[img_index]
        # tag = process_tags(self.vocab, tag)
        # Convert caption (string) to word ids (with Size Augmentation at training time).
        target = process_caption(self.vocab, caption, self.train)
        image = self.images[img_index]
        # bboxes = self.bbox[img_index]
        # imsize = self.sizes[img_index]
        # k = image.shape[0]
        # for i in range(k):
        #     bbox = bboxes[i]
        #     bbox[0] /= imsize['image_w']
        #     bbox[1] /= imsize['image_h']
        #     bbox[2] /= imsize['image_w']
        #     bbox[3] /= imsize['image_h']
        #     bboxes[i] = bbox
        box = torch.tensor(self.boxes[img_index])

        if self.train:  # Size augmentation on region features.
            if self.opt.schema == 'random':
                num_features = image.shape[0]
                rand_list = np.random.rand(num_features)
                image = image[np.where(rand_list > self.obj_drop_rate)]
                box = box[np.where(rand_list > self.obj_drop_rate)]
            # elif self.opt.schema == 'tags':
            #     unique_tag = {}
            #     for i, v in enumerate(tag):
            #         if v not in unique_tag:
            #             unique_tag[i] = 1
            #         else :
            #             unique_tag[i] += 1
            #     image = image[[key for key in unique_tag.keys()]]
            #     box = box[[key for key in unique_tag.keys()]]
            # elif self.opt.schema == 'center':
            #     num_features = image.shape[0]
            #     n_r = 26
            #     bb_size = (bboxes[:, 2:] - bboxes[:, :2])
            #     bb_centre = bboxes[:, :2] + 0.5 * bb_size
            #     bb_centre = torch.tensor(bb_centre)
            #     centre_coord = bb_centre.mean(dim=0, keepdim=True).repeat_interleave(num_features, 0)
            #     pseudo_coord = bb_centre - centre_coord
            #     rho = torch.sqrt(pseudo_coord[:, 0]**2 + pseudo_coord[:, 1]**2)
            #     rho_index = rho.sort(descending=False, dim=-1)[1]
            #     image = image[rho_index[:n_r]]
            #     box = box[rho_index[:n_r]]
        image = torch.Tensor(image)
        box = torch.tensor(box)
        return image, target, index, img_index, box

    def __len__(self):
        return self.length


def process_caption(vocab, caption, drop=False):
    if not drop:
        words = nltk.tokenize.word_tokenize(caption.lower())
        caption = list()
        caption.append(vocab('<start>'))
        caption.extend([vocab(token) for token in words])
        caption.append(vocab('<end>'))
        target = torch.Tensor(caption)
        return target
    else:
        # Convert caption (string) to word ids.
        tokens = ['<start>', ]
        words = nltk.tokenize.word_tokenize(caption.lower())
        tokens.extend(words)
        tokens.append('<end>')
        deleted_idx = []
        for i, token in enumerate(tokens):
            prob = random.random()
            if prob < 0.20:
                prob /= 0.20
                # 50% randomly change token to mask token
                if prob < 0.5:
                    tokens[i] = vocab.word2idx['<mask>']
                # 10% randomly change token to random token
                elif prob < 0.6:
                    tokens[i] = random.randrange(len(vocab))
                # 40% randomly remove the token
                else:
                    tokens[i] = vocab(token)
                    deleted_idx.append(i)
            else:
                tokens[i] = vocab(token)
        if len(deleted_idx) != 0:
            tokens = [tokens[i] for i in range(len(tokens)) if i not in deleted_idx]
        target = torch.Tensor(tokens)
        return target
    
def process_tags(vocab, tags):

    words = tags.strip().split(',')[:36]
    caption = list()
    caption.extend([vocab(token) for token in words])
    target = torch.tensor(caption, dtype=torch.long)
    return target


def collate_fn(data):
    """Build mini-batch tensors from a list of (image, caption) tuples.
    Args:
        data: list of (image, caption) tuple.
            - image: torch tensor of shape (3, 256, 256).
            - caption: torch tensor of shape (?); variable length.

    Returns:
        images: torch tensor of shape (batch_size, 3, 256, 256).
        targets: torch tensor of shape (batch_size, padded_length).
        lengths: list; valid length for each padded caption.
    """
    # Sort a data list by caption length
    data.sort(key=lambda x: len(x[1]), reverse=True)
    images, captions, ids, img_ids, boxes = zip(*data)
    if len(images[0].shape) == 2:  # region feature
        # Merge images
        img_lengths = [len(image) for image in images]
        box_lengths = [len(box) for box in boxes]
        all_images = torch.zeros(len(images), max(img_lengths), images[0].size(-1))
        all_boxes = torch.zeros(len(boxes), max(box_lengths), boxes[0].size(-1))
        for i, image in enumerate(images):
            end = img_lengths[i]
            all_images[i, :end] = image[:end]
        for i, box in enumerate(boxes):
            end = box_lengths[i]
            all_boxes[i, :end] = box[:end]
        img_lengths = torch.Tensor(img_lengths)
        box_lengths = torch.Tensor(box_lengths)

        # Merget captions
        lengths = [len(cap) for cap in captions]
        targets = torch.zeros(len(captions), max(lengths)).long()

        for i, cap in enumerate(captions):
            end = lengths[i]
            targets[i, :end] = cap[:end]

        return all_images, img_lengths, targets, lengths, all_boxes, ids
    else:  # raw input image
        # Merge images
        images = torch.stack(images, 0)
        # Merget captions
        lengths = [len(cap) for cap in captions]
        targets = torch.zeros(len(captions), max(lengths)).long()
        for i, cap in enumerate(captions):
            end = lengths[i]
            targets[i, :end] = cap[:end]
        return images, targets, lengths, ids


def get_loader(data_path, data_name, data_split, vocab, opt, batch_size=100,
               shuffle=True, num_workers=2, train=True):
    """Returns torch.utils.data.DataLoader for custom coco dataset."""
    if train:
        drop_last = True
    else:
        drop_last = False
    if opt.precomp_enc_type in ["basic","selfattention","transformer"]:
        dset = PrecompRegionDataset(data_path, data_name, data_split, vocab, opt, train)
        data_loader = torch.utils.data.DataLoader(dataset=dset,
                                                  batch_size=batch_size,
                                                  shuffle=shuffle,
                                                  pin_memory=True,
                                                  collate_fn=collate_fn,
                                                  num_workers=num_workers,
                                                  drop_last=drop_last)
    else:
        raise ValueError("Unknown precomp_enc_type: {}".format(opt.precomp_enc_type))
    return data_loader


def get_loaders(data_path, data_name, vocab, batch_size, workers, opt):
    train_loader = get_loader(data_path, data_name, 'train', vocab, opt,
                              batch_size, True, workers, train=opt.drop)
    val_loader = get_loader(data_path, data_name, 'dev', vocab, opt,
                            batch_size, False, workers, train=False)
    return train_loader, val_loader


def get_train_loader(data_path, data_name, vocab, batch_size, workers, opt, shuffle):
    train_loader = get_loader(data_path, data_name, 'train', vocab, opt,
                              batch_size, shuffle, workers)
    return train_loader


def get_test_loader(split_name, data_name, vocab, batch_size, workers, opt):
    test_loader = get_loader(opt.data_path, data_name, split_name, vocab, opt,
                             batch_size, False, workers, train=False)
    return test_loader
