from __future__ import division
import pdb
import random
import os
import time
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn import functional as F

from data import * 
from models import * 
from args import * 

def minibatch_generator(dataset, args, shuffle=True):
    """
    Generator used to feed the minibatches
    """
    PAD_token = 0
    SOS_token = 2
    total_words = sum([len(x) for x in dataset])

    if args.stream_data:
        # ASSUMES the given dataset is an ORDERED sequence of sentences. 

        if args.max_seq_len is None: 
            raise ValueError('a sentence length parameter (max_seq_len) is required when data is streamed')

        # TODO: don't hardcode the 10k
        words_per_minibatch = args.batch_size * (args.max_seq_len + 1)
        num_batches = max(1, total_words // words_per_minibatch)
    
        # we need to calculate what is the last sentence index we can reach; for example, 
        # if our dataset has sentences of length 20, and max_seq_len == 100, then we can't 
        # start a minibatch pt with the last 4 sentences, as we would run out of words. 
        last_available_sentence = len(dataset) 
        words_needed = args.max_seq_len
        while words_needed > 0: 
            last_available_sentence -= 1
            assert last_available_sentence >= 0, 'not enough data to generate a single sentence!'
            words_needed -= len(dataset[last_available_sentence])

        current_batch = 0
        sentence_index = 0
        while current_batch < num_batches: 
            current_batch += 1
            batch = []
            while len(batch) < args.batch_size:
                if shuffle: 
                    ind = np.random.randint(last_available_sentence + 1)
                else: 
                    ind = sentence_index % (last_available_sentence + 1)
                    sentence_index += 1

                # now, we fetch the correct amt of words from the sentence `ind` points to. 
                current_sentence = []
                while len(current_sentence) != args.max_seq_len + 1: 
                    indexed_sentence = dataset[ind]
                    words_taken = min(args.max_seq_len + 1 - len(current_sentence), \
                                      len(indexed_sentence))

                    current_sentence += indexed_sentence[:words_taken]

                batch += [current_sentence]
            
            batch_src = torch.LongTensor(batch).view(args.batch_size, args.max_seq_len + 1)
            input  = batch_src[:, :-1]
            target = batch_src[:, 1:]
            len_s  = [args.max_seq_len] * args.batch_size
            len_s  = torch.LongTensor(len_s)

            input  = Variable(input)
            target = Variable(target)
            
            if args.cuda:
                input = input.cuda()
                target = target.cuda()
                len_s = len_s.cuda()
            
            yield input, target, len_s 
            
    else: 
        def fill_seq(input, padded_length, fill_token):
            input_padded = input[:]
            input_padded += [fill_token] * (padded_length - len(input))
            return input_padded

        nb_elem = len(dataset)
        indices = list(range(nb_elem))
        if shuffle:
            random.shuffle(indices)

        while nb_elem > 0:

            b_, len_ = [], []

            count = 0
            while count < args.batch_size and nb_elem > 0:
                ind = indices.pop()
                count += 1
                nb_elem -= 1

                b_.append(dataset[ind])
                len_.append(len(dataset[ind]))
            
            max_ = args.max_seq_len if args.max_seq_len is not None else max(len_)
            if args.mask_padding: 
                len_ = [ min(x, max_) for x in len_]
            else: 
                len_ = [max_ for _ in len_]

            b_   = [sentence[:max_] for sentence in b_]    

            # we need to fill shorter sentences to make tensor
            b__ = [fill_seq(seq, max_, PAD_token) for seq in b_]

            # sort the lists by len_src for pack_padded_sentence later
            b_sorted = [(x,l) for (x,l) in \
                           sorted(zip(b__, len_),
                                  key=lambda v: v[1],  # using len_src
                                  reverse=True)]  # descending order

            # unzip to individual lists
            b_s, len_s = zip(*b_sorted)

            # create pytorch variable, transpose to have (seq, batch)
            batch_src = torch.LongTensor(b_s)

            # create the target
            if True: # args.use_sos_token :
                target = batch_src
                input = torch.zeros(batch_src.size())
                input[:, 0] = SOS_token
                input[:, 1:] = target[:, :target.size(1) - 1]
            else: 
                # we cut the length of each sentence by one
                target = batch_src[:, 1:]
                input  = batch_src[:, :-1]
                len_s  = [x - 1 for x in len_s]
            
            input = Variable(input.long())
            target = Variable(target.long())
            len_s = torch.LongTensor(len_s)

            if args.cuda:
                input = input.cuda()
                target = target.cuda()
                len_s = len_s.cuda()

            yield input, target, len_s
        

def get_cumulative_rewards(disc_logits, args, is_already_reward=False):
    # disc_logits : bs x seq_len 
    assert len(disc_logits.size()) == 2
    if is_already_reward: 
        rewards = disc_logits
    else: 
        rewards = F.sigmoid(disc_logits + 1e-7)
        rewards = torch.log(rewards + 1e-7)

    bs, seq_len = rewards.size()
    cumulative_rewards = torch.zeros_like(rewards)
    for t in reversed(range(seq_len)):
        if t == seq_len - 1: 
            cumulative_rewards[:, t] = rewards[:, t]
            # if in SEQGAN mode, make sure reward only comes from the last timestep
            if args.seqgan_reward: rewards = rewards * 0. 
        else:
            cumulative_rewards[:, t] = rewards[:, t] + args.gamma * cumulative_rewards[:, t+1]

    return cumulative_rewards


def id_to_words(tensor, word_dict):
    assert type(tensor) == np.ndarray
    assert tensor.ndim  == 2
    sentences = []
    for sentence in tensor:
        human_readable = []
        for word in sentence:
            human_readable.append(word_dict.idx2word[word])
        human_readable = ' '.join(human_readable)
        human_readable = human_readable.replace('<eos>','.').replace(' <pad>','')
        human_readable = human_readable.replace('<qm>','?').replace('<em>','!')
        sentences.append(human_readable)
    return sentences


def apply_loss(optimizer, loss, retain_graph=False, clip_norm=None, stop=False):
    optimizer.zero_grad()
    loss.backward(retain_graph=retain_graph)
    if clip_norm is not None: 
        params = optimizer.param_groups[0]['params']
        torch.nn.utils.clip_grad_norm_(params, clip_norm)
    if stop: pdb.set_trace()
    optimizer.step()


def print_and_log_scalar(writer, name, value, write_no, end_token=''):
    if isinstance(value, list):
        if len(value) == 0: return 
        value = torch.mean(torch.stack(value))
    zeros = 40 - len(name) 
    name += ' ' * zeros
    print('{} @ write {} = {:.4f}{}'.format(name, write_no, value, end_token))
    writer.add_scalar(name, value, write_no)


def assign_training(iteration, epoch, args):
    # returns should_train_gen, should_train_disc, should_train_mle
    if epoch < args.disc_pretrain_epochs:
        return False, True, False

    gti = args.gen_train_iterations
    dti = args.disc_train_iterations
    mti = args.mle_train_iterations
    total = dti + gti + mti
    res = iteration % total
    if   res        <  gti: 
        return True, False, False
    elif gti        <= res < gti + dti: 
        return False, True, False
    elif gti + dti  <= res + gti + dti + mti:   
        return False, False, True
    else:
        raise Exception('should not get here')


def generate_file(gen, first_token, name='output.txt'):
    num_rounds = 10000 // first_token.size(0) + 1
    output = []
    for _ in range(num_rounds):
        sentence = gen(first_token)[1].data
        output.append(sentence)

    output = torch.cat(output, dim=0).cpu().numpy()
    output = output[:10000]
    with open(name, 'w') as f: 
        for line in output:
            xx = str(line)[1:-1]
            xx = xx.replace('\n', '')
            f.write(xx + '\n')


def print_and_save_samples(fake_sentences, word_dict, base_dir, epoch, max_print=5):
    print('samples generated after %d epochs' % epoch)
    file_name = os.path.join(base_dir, 'samples/generated{}.txt'.format(epoch))
    sentences = id_to_words(fake_sentences.cpu().data.numpy(), word_dict)
    with open(file_name, 'w') as f:
        for i, sentence in enumerate(sentences): 
            xx = str(sentence) #[1:-1]
            if i <  max_print: print(xx)
            if i == max_print: print('\n')
            xx = xx.replace('\n', '')
            f.write(xx + '\n')


def save_models(models, base_dir, epoch):
    for model_ in models:
        name, model, opt = model_
        save_name = os.path.join(os.path.join(base_dir, 'models'), name + str(epoch))
        try: 
            torch.save(model.state_dict(), save_name + '.pth')
        except: 
            assert 'critic' in name or model is None 
        torch.save(opt.state_dict(), save_name + 'opt.pth')
    print('saved {} models'.format(len(models)))
    

def print_and_save_args(args, path):
    print(args)
    # let's save the args as json to enable easy loading
    import json
    with open(os.path.join(path, 'args.json'), 'w') as f: 
        json.dump(vars(args), f)

def maybe_create_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def to_attr(args_dict):
    class AttrDict(dict):
        def __init__(self, *args, **kwargs):
            super(AttrDict, self).__init__(*args, **kwargs)
            self.__dict__ = self

    return AttrDict(args_dict)

def remove_pad_tokens(tensor, index):
    assert len(tensor.shape) == 1 and len(index.shape) == 1
    is_not_pad = (index != 0).float()
    summ = (tensor * is_not_pad).sum(dim=0)
    is_not_pad = is_not_pad.sum()
    if is_not_pad == 0: 
        print('batch is only PAD token')
        return summ * 0. + -.1
    else: 
        print('batch contains %d / %d active sentences' % (is_not_pad.item(), tensor.size(0)))

    return summ / is_not_pad
    
       

def get_oracle(args):
    args_dict = vars(args).copy()
    args_copy = to_attr(args_dict)
    args_copy.num_layers_gen = 1
    args_copy.hidden_dim_gen = 32
    args_copy.rnn = 'LSTM'
    oracle =  Generator(args_copy, is_oracle=True)
    for p in oracle.parameters(): p.data.normal_(0, 1000000)
    oracle = oracle.eval()
    return oracle


def load_model_from_file(path, args=None, epoch=None):
    import json
    with open(os.path.join(path, 'args.json'), 'r') as f: 
        old_args = json.load(f)
    
    if args is None: args = get_train_args(allow_unmatched_args=True)[0]

    args_dict = vars(args)
    for key in args_dict.keys():
        if key not in old_args:
            print('Warning: new arg \'{}\' given value \'{}\''.format(key, args_dict[key]))
            old_args[key] = args_dict[key]

    old_args = to_attr(old_args)
    gen = Generator(old_args)

    if epoch is None: # get last model
        all_ = os.listdir(os.path.join(path, 'models'))
        all_ = [x[3:-4] for x in all_ if 'gen' in x and 'opt' not in x]
        epochs = sorted([int(x) for x in all_])
        if len(epochs) == 0 : 
            raise FileNotFoundError('no model files were found in %s' % path)
        
        epoch = epochs[-1]
       
    gen.load_state_dict(torch.load(os.path.join(path, 'models/gen%d.pth' % epoch)))
    print('model successfully loaded')

    return gen, epoch

def transfer_weights(gen, disc):
    # 1) transfer embedding
    disc.embedding.weight.data.copy_(gen.embedding.weight.data)
    # disc.embedding.requires_grad = False
    # 2) transfer RNN weights
    for rnn_disc, rnn_gen in zip(disc.rnns, gen.rnns):
        rnn_disc.load_state_dict(rnn_gen.state_dict())
        # rnn_disc.requires_grad = False
