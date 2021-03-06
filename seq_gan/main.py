# -*- coding:utf-8 -*-

# custom import
import sys
sys.path.insert(0, '../core')

from helper import *
# 
import os
import random
import math
import argparse
import tqdm

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable

from generator import Generator
from discriminator import Discriminator
from target_lstm import TargetLSTM
from rollout import Rollout
from data_iter import GenDataIter, DisDataIter
# ================== Parameter Definition =================

parser = argparse.ArgumentParser(description='Training Parameter')
parser.add_argument('--cuda', action='store', default=None, type=int)
parser.add_argument('--test', action='store_true')
opt = parser.parse_args()

# Basic Training Paramters
SEED = 88
BATCH_SIZE = 10
TOTAL_BATCH = 500
GENERATED_NUM = 1000
ROOT_PATH =  'experiments/real_samples/1_100/'
POSITIVE_FILE = ROOT_PATH + 'real.data'
TEST_FILE     = ROOT_PATH + 'test.data'
NEGATIVE_FILE = ROOT_PATH + 'gene.data'
DEBUG_FILE = ROOT_PATH + 'debug.data'
EVAL_FILE = ROOT_PATH + 'eval.data'
VOCAB_SIZE = 5000
PRE_EPOCH_NUM = 50
CHECKPOINT_PATH = ROOT_PATH + 'checkpoints/'

try:  
    os.makedirs(CHECKPOINT_PATH)
except OSError:  
    print('Directory already exists!')

if opt.cuda is not None and opt.cuda >= 0:
    torch.cuda.set_device(opt.cuda)
    opt.cuda = True

# Genrator Parameters
g_emb_dim = 32
g_hidden_dim = 32
g_sequence_len = 11

# Discriminator Parameters
d_emb_dim = 64
d_filter_sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] #, 15, 20]
d_num_filters = [100, 200, 200, 200, 200, 100, 100, 100, 100, 100] #, 160, 160]

d_dropout = 0.75
d_num_class = 2

def demo():
    idx_to_word, word_to_idx, VOCAB_SIZE = load_vocab(CHECKPOINT_PATH)
    test_iter = GenDataIter(TEST_FILE, BATCH_SIZE)
    generator = Generator(VOCAB_SIZE, g_emb_dim, g_hidden_dim, opt.cuda)
    generator = generator.cuda()
    generator.load_state_dict(torch.load(CHECKPOINT_PATH+'generator.model'))
    test_predict(generator, test_iter, idx_to_word)

def get_word(s, idx_to_words = None):
    if idx_to_words == None:
        return str(s)
    return idx_to_words[int(s)]

def generate_samples(model, batch_size, generated_num, output_file, idx_to_word = None):
    samples = []
    for _ in range(int(generated_num / batch_size)):
        sample = model.sample(batch_size, g_sequence_len).cpu().data.numpy().tolist()
        # for each_sen in sample:
        #    generate_sentence_from_id(idx_to_word, each_sen, DEBUG_FILE, header = 'REAL ---')
        samples.extend(sample)
    with open(output_file, 'w') as fout:
        for sample in samples:
            string = ' '.join([str(s) for s in sample])
            fout.write('%s\n' % string)

def train_epoch(model, data_iter, criterion, optimizer):
    total_loss = 0.
    total_words = 0.
    for (data, target) in data_iter:#tqdm(
        #data_iter, mininterval=2, desc=' - Training', leave=False):
        data = Variable(data)
        target = Variable(target)
        if opt.cuda:
            data, target = data.cuda(), target.cuda()
        target = target.contiguous().view(-1)
        pred = model.forward(data)
        if len(pred.shape) > 2:
            pred = torch.reshape(pred, (pred.shape[0] * pred.shape[1], -1))
        loss = criterion(pred, target)
        total_loss += loss.data.item()
        total_words += data.size(0) * data.size(1)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    data_iter.reset()
    return math.exp(total_loss / total_words)

def eval_epoch(model, data_iter, criterion):
    total_loss = 0.
    total_words = 0.
    for (data, target) in data_iter:#tqdm(
        #data_iter, mininterval=2, desc=' - Training', leave=False):
        data = Variable(data, volatile=True)
        target = Variable(target, volatile=True)
        if opt.cuda:
            data, target = data.cuda(), target.cuda()
        target = target.contiguous().view(-1)
        pred = model.forward(data)
        loss = criterion(pred, target)
        total_loss += loss.data.item()
        total_words += data.size(0) * data.size(1)
    data_iter.reset()
    return math.exp(total_loss / total_words)

def test_predict(model, data_iter, idx_to_word, train_mode = False):
    data_iter.reset()
    for (data, target) in data_iter:
        data = Variable(data, volatile=True)
        target = Variable(target, volatile=True)
        if opt.cuda:
            data, target = data.cuda(), target.cuda()
        target = target.contiguous().view(-1)
        prob = model.forward(data)
        mini_batch = prob.shape[0]
        if len(prob.shape) > 2:
            prob = torch.reshape(prob, (prob.shape[0] * prob.shape[1], -1))
        else:
            mini_batch /= g_sequence_len
            mini_batch = int(mini_batch)
        predictions = torch.max(prob, dim=1)[1]
        predictions = predictions.view(mini_batch, -1)
        # print('PRED SHAPE:' , predictions.shape)
        for each_sen in list(predictions):
            print('Sample Test Output:', generate_sentence_from_id(idx_to_word, each_sen))
        sys.stdout.flush()
        if train_mode:
            break
    data_iter.reset()

class GANLoss(nn.Module):
    """Reward-Refined NLLLoss Function for adversial training of Gnerator"""
    def __init__(self):
        super(GANLoss, self).__init__()

    def forward(self, prob, target, reward):
        """
        Args:
            prob: (N, C), torch Variable 
            target : (N, ), torch Variable
            reward : (N, ), torch Variable
        """
        N = target.size(0)
        C = prob.size(1)
        one_hot = torch.zeros((N, C))
        if prob.is_cuda:
            one_hot = one_hot.cuda()
        one_hot.scatter_(1, target.data.view((-1,1)), 1)
        one_hot = one_hot.type(torch.ByteTensor)
        one_hot = Variable(one_hot)
        if prob.is_cuda:
            one_hot = one_hot.cuda()
        loss = torch.masked_select(prob, one_hot)
        loss = loss * reward
        loss =  -torch.sum(loss)
        return loss


def main():
    random.seed(SEED)
    np.random.seed(SEED)
    
    # Build up dataset
    s_train, s_test = load_from_big_file('../data/train_data_obama.txt')
    # idx_to_word: List of id to word
    # word_to_idx: Dictionary mapping word to id
    idx_to_word, word_to_idx = fetch_vocab(s_train, s_train, s_test)
    # input_seq, target_seq = prepare_data(DATA_GERMAN, DATA_ENGLISH, word_to_idx)

    global VOCAB_SIZE
    VOCAB_SIZE = len(idx_to_word)
    save_vocab(CHECKPOINT_PATH+'metadata.data', idx_to_word, word_to_idx, VOCAB_SIZE, g_emb_dim, g_hidden_dim)


    print('VOCAB SIZE:' , VOCAB_SIZE)
    # Define Networks
    generator = Generator(VOCAB_SIZE, g_emb_dim, g_hidden_dim, opt.cuda)
    discriminator = Discriminator(d_num_class, VOCAB_SIZE, d_emb_dim, d_filter_sizes, d_num_filters, d_dropout)
    target_lstm = TargetLSTM(VOCAB_SIZE, g_emb_dim, g_hidden_dim, opt.cuda)
    if opt.cuda:
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        target_lstm = target_lstm.cuda()
    # Generate toy data using target lstm
    print('Generating data ...')
    
    # Generate samples either from sentences file or lstm
    # Sentences file will be structured input sentences
    # LSTM based is BOG approach
    generate_real_data('../data/train_data_obama.txt', BATCH_SIZE, GENERATED_NUM, idx_to_word, word_to_idx, POSITIVE_FILE, TEST_FILE)
    # generate_samples(target_lstm, BATCH_SIZE, GENERATED_NUM, POSITIVE_FILE, idx_to_word)
    # generate_samples(target_lstm, BATCH_SIZE, 10, TEST_FILE, idx_to_word)
    # Create Test data iterator for testing
    test_iter = GenDataIter(TEST_FILE, BATCH_SIZE)
    
    # Load data from file
    gen_data_iter = GenDataIter(POSITIVE_FILE, BATCH_SIZE)

    # Pretrain Generator using MLE
    gen_criterion = nn.NLLLoss(size_average=False)
    gen_optimizer = optim.Adam(generator.parameters())
    if opt.cuda:
        gen_criterion = gen_criterion.cuda()
    print('Pretrain with MLE ...')
    for epoch in range(PRE_EPOCH_NUM):
        loss = train_epoch(generator, gen_data_iter, gen_criterion, gen_optimizer)
        print('Epoch [%d] Model Loss: %f'% (epoch, loss))
        sys.stdout.flush()

        # generate_samples(generator, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
        # eval_iter = GenDataIter(EVAL_FILE, BATCH_SIZE)
        # loss = eval_epoch(target_lstm, eval_iter, gen_criterion)
        # print('Epoch [%d] True Loss: %f' % (epoch, loss))

    # Pretrain Discriminator
    dis_criterion = nn.NLLLoss(size_average=False)
    dis_optimizer = optim.Adam(discriminator.parameters())
    if opt.cuda:
        dis_criterion = dis_criterion.cuda()
    print('Pretrain Discriminator ...')
    for epoch in range(3):
        generate_samples(generator, BATCH_SIZE, GENERATED_NUM, NEGATIVE_FILE)
        dis_data_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, BATCH_SIZE)
        for _ in range(3):
            loss = train_epoch(discriminator, dis_data_iter, dis_criterion, dis_optimizer)
            print('Epoch [%d], loss: %f' % (epoch, loss))
            sys.stdout.flush()

    # Adversarial Training 
    rollout = Rollout(generator, 0.8)
    print('#####################################################')
    print('Start Adversarial Training...\n')
    gen_gan_loss = GANLoss()
    gen_gan_optm = optim.Adam(generator.parameters())
    if opt.cuda:
        gen_gan_loss = gen_gan_loss.cuda()
    gen_criterion = nn.NLLLoss(size_average=False)
    if opt.cuda:
        gen_criterion = gen_criterion.cuda()
    dis_criterion = nn.NLLLoss(size_average=False)
    dis_optimizer = optim.Adam(discriminator.parameters())
    if opt.cuda:
        dis_criterion = dis_criterion.cuda()
    for total_batch in range(TOTAL_BATCH):
        ## Train the generator for one step
        for it in range(1):
            samples = generator.sample(BATCH_SIZE, g_sequence_len)
            # construct the input to the genrator, add zeros before samples and delete the last column
            zeros = torch.zeros((BATCH_SIZE, 1)).type(torch.LongTensor)
            if samples.is_cuda:
                zeros = zeros.cuda()
            inputs = Variable(torch.cat([zeros, samples.data], dim = 1)[:, :-1].contiguous())
            targets = Variable(samples.data).contiguous().view((-1,))
            # calculate the reward
            rewards = rollout.get_reward(samples, 16, discriminator)
            rewards = Variable(torch.Tensor(rewards))
            if opt.cuda:
                rewards = torch.exp(rewards.cuda()).contiguous().view((-1,))
            prob = generator.forward(inputs)
            # print('SHAPE: ', prob.shape, targets.shape, rewards.shape)
            loss = gen_gan_loss(prob, targets, rewards)
            gen_gan_optm.zero_grad()
            loss.backward()
            gen_gan_optm.step()
            # print('GEN PRED DIM: ', prob.shape)
            

        if total_batch % 10 == 0 or total_batch == TOTAL_BATCH - 1:
            # generate_samples(generator, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
            # eval_iter = GenDataIter(EVAL_FILE, BATCH_SIZE)
            # loss = eval_epoch(target_lstm, eval_iter, gen_criterion)
            # print('Batch [%d] True Loss: %f' % (total_batch, loss))

            predictions = torch.max(prob, dim=1)[1]
            predictions = predictions.view(BATCH_SIZE, -1)
            # print('PRED SHAPE:' , predictions.shape)
            for each_sen in list(predictions):
                print('Training Output:', generate_sentence_from_id(idx_to_word, each_sen, DEBUG_FILE))
            
            test_predict(generator, test_iter, idx_to_word, train_mode = True)
            
            sys.stdout.flush()

            torch.save(generator.state_dict(), CHECKPOINT_PATH + 'generator_seqgan.model')
            torch.save(discriminator.state_dict(), CHECKPOINT_PATH + 'discriminator_seqgan.model')
        rollout.update_params()
        
        for _ in range(4):
            generate_samples(generator, BATCH_SIZE, GENERATED_NUM, NEGATIVE_FILE)
            dis_data_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, BATCH_SIZE)
            for _ in range(2):
                loss = train_epoch(discriminator, dis_data_iter, dis_criterion, dis_optimizer)
if __name__ == '__main__':
    if opt.test:
        demo()
        exit()
    main()
