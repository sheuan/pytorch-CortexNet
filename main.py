import argparse
import os
import os.path as path
import time
from datetime import timedelta
from sys import exit, argv

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable as V
from torch.utils.data import DataLoader
from torchvision import transforms as trn

from data.VideoFolder import VideoFolder, BatchSampler, VideoCollate
from utils.image_plot import show_four, show_ten

parser = argparse.ArgumentParser(description='PyTorch MatchNet generative model training script',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
_ = parser.add_argument  # define add_argument shortcut
_('--data', type=str, default='./data/processed-data', help='location of the video data')
_('--model', type=str, default='CortexNet', help='type of auto-encoder')
_('--mode', type=str, required=True, help='training mode [MatchNet|TempoNet]')
_('--size', type=int, default=(3, 32, 64, 128, 256), nargs='*', help='number and size of hidden layers', metavar='S')
_('--spatial-size', type=int, default=(256, 256), nargs=2, help='frame cropping size', metavar=('H', 'W'))
_('--lr', type=float, default=0.1, help='initial learning rate')
_('--momentum', type=float, default=0.9, metavar='M', help='momentum')
_('--weight-decay', type=float, default=1e-4, metavar='W', help='weight decay')
_('--mu', type=float, default=1, help='matching MSE multiplier', dest='mu', metavar='μ')
_('--tau', type=float, default=0.1, help='temporal CE multiplier', dest='tau', metavar='τ')
_('--pi', default='τ', help='periodical CE multiplier', dest='pi', metavar='π')
_('--epochs', type=int, default=10, help='upper epoch limit')
_('--batch-size', type=int, default=20, metavar='B', help='batch size')
_('--big-t', type=int, default=10, help='sequence length', metavar='T')
_('--seed', type=int, default=0, help='random seed')
_('--log-interval', type=int, default=10, metavar='N', help='report interval')
_('--save', type=str, default='last/model.pth.tar', help='path to save the final model')
_('--cuda', action='store_true', help='use CUDA')
_('--view', type=int, default=tuple(), help='samples to view at the end of every log-interval batches', metavar='V')
_('--show-x_hat', action='store_true', help='show x_hat')
_('--lr-decay', type=float, default=None, nargs=2, metavar=('D', 'E'),
  help='decay of D (e.g. 3.16, 10) times, every E (e.g. 3) epochs')
_('--pre-trained', type=str, default='', help='path to pre-trained model', metavar='P')
args = parser.parse_args()
args.size = tuple(args.size)  # cast to tuple
if args.lr_decay: args.lr_decay = tuple(args.lr_decay)
if type(args.view) is int: args.view = (args.view,)  # cast to tuple
args.pi = args.tau if args.pi == 'τ' else float(args.pi)

# Print current options
print('CLI arguments:', ' '.join(argv[1:]))

# Print current commit
if path.isdir('.git'):  # if we are in a repo
    with os.popen('git rev-parse HEAD') as pipe:  # get the HEAD's hash
        print('Current commit hash:', pipe.read(), end='')

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)


def main():
    # Load data
    print('Define image pre-processing')
    # normalise? do we care?
    t = trn.Compose((trn.ToPILImage(), trn.CenterCrop(args.spatial_size), trn.ToTensor()))

    print('Define train data loader')
    train_data_name = 'train_data.tar'
    if os.access(train_data_name, os.R_OK):
        train_data = torch.load(train_data_name)
    else:
        train_path = path.join(args.data, 'train')
        if args.mode == 'MatchNet':
            train_data = VideoFolder(root=train_path, transform=t, video_index=True)
        elif args.mode == 'TempoNet':
            train_data = VideoFolder(root=train_path, transform=t, shuffle=True)
        torch.save(train_data, train_data_name)

    train_loader = DataLoader(
        dataset=train_data,
        batch_size=args.batch_size * args.big_t,  # batch_size rows and T columns
        shuffle=False,
        sampler=BatchSampler(data_source=train_data, batch_size=args.batch_size),  # given that BatchSampler knows it
        num_workers=1,
        collate_fn=VideoCollate(batch_size=args.batch_size),
        pin_memory=True
    )

    print('Define validation data loader')
    val_data_name = 'val_data.tar'
    if os.access(val_data_name, os.R_OK):
        val_data = torch.load(val_data_name)
    else:
        val_path = path.join(args.data, 'val')
        if args.mode == 'MatchNet':
            val_data = VideoFolder(root=val_path, transform=t, video_index=True)
        elif args.mode == 'TempoNet':
            val_data = VideoFolder(root=val_path, transform=t, shuffle='init')
        torch.save(val_data, val_data_name)

    val_loader = DataLoader(
        dataset=val_data,
        batch_size=args.batch_size,  # just one column of size batch_size
        shuffle=False,
        sampler=BatchSampler(data_source=val_data, batch_size=args.batch_size),
        num_workers=1,
        collate_fn=VideoCollate(batch_size=args.batch_size),
        pin_memory=True
    )

    # Build the model
    if args.model == 'model_01':
        from model.Model01 import Model01 as Model
    elif args.model == 'model_02' or args.model == 'CortexNet':
        from model.Model02 import Model02 as Model
    elif args.model == 'model_02_rg':
        from model.Model02 import Model02RG as Model
    else:
        print('\n{:#^80}\n'.format(' Please select a valid model '))
        exit()

    print('Define model')
    if args.mode == 'MatchNet':
        nb_train_videos = len(train_data.videos)
        model = Model(args.size + (nb_train_videos,), args.spatial_size)
    elif args.mode == 'TempoNet':
        nb_classes = len(train_data.classes)
        model = Model(args.size + (nb_classes,), args.spatial_size)

    if args.pre_trained:
        print('Load pre-trained weights')
        # args.pre_trained = 'model/model02D-33IS/model_best.pth.tar'
        dict_33 = torch.load(args.pre_trained)['state_dict']

        def load_state_dict(new_model, state_dict):
            own_state = new_model.state_dict()
            for name, param in state_dict.items():
                name = name[19:]  # remove 'module.inner_model.' part
                if name not in own_state:
                    raise KeyError('unexpected key "{}" in state_dict'
                                   .format(name))
                if name.startswith('stabiliser'):
                    print('Skipping', name)
                    continue
                if isinstance(param, nn.Parameter):
                    # backwards compatibility for serialized parameters
                    param = param.data
                own_state[name].copy_(param)

            missing = set(own_state.keys()) - set([k[19:] for k in state_dict.keys()])
            if len(missing) > 0:
                raise KeyError('missing keys in state_dict: "{}"'.format(missing))

        load_state_dict(model, dict_33)

    print('Create a MSE and balanced NLL criterions')
    mse = nn.MSELoss()

    # independent CE computation
    nll_final = nn.CrossEntropyLoss(size_average=False)
    # balance classes based on frames per video; default balancing weight is 1.0f
    w = torch.Tensor(train_data.frames_per_video if args.mode == 'MatchNet' else train_data.frames_per_class)
    w.div_(w.mean()).pow_(-1)
    nll_train = nn.CrossEntropyLoss(w)
    w = torch.Tensor(val_data.frames_per_video if args.mode == 'MatchNet' else val_data.frames_per_class)
    w.div_(w.mean()).pow_(-1)
    nll_val = nn.CrossEntropyLoss(w)

    if args.cuda:
        model.cuda()
        mse.cuda()
        nll_final.cuda()
        nll_train.cuda()
        nll_val.cuda()

    print('Instantiate a SGD optimiser')
    optimiser = optim.SGD(
        params=model.parameters(),
        lr=args.lr,
        momentum=args.momentum,
        weight_decay=args.weight_decay
    )

    # Loop over epochs
    for epoch in range(0, args.epochs):
        if args.lr_decay: adjust_learning_rate(optimiser, epoch)
        epoch_start_time = time.time()
        train(train_loader, model, (mse, nll_final, nll_train), optimiser, epoch)
        print(80 * '-', '| end of epoch {:3d} |'.format(epoch + 1), sep='\n', end=' ')
        val_loss = validate(val_loader, model, (mse, nll_final, nll_val))
        elapsed_time = str(timedelta(seconds=int(time.time() - epoch_start_time)))  # HH:MM:SS time format
        print('time: {} | mMSE {:.2e} | CE {:.2e} | rpl mMSE {:.2e} | per CE {:.2e} |'.
              format(elapsed_time, val_loss['mse'] * 1e3, val_loss['ce'], val_loss['rpl'] * 1e3, val_loss['per_ce']))
        print(80 * '-')

        if args.save != '':
            torch.save(model, args.save)


def adjust_learning_rate(opt, epoch):
    """Sets the learning rate to the initial LR decayed by D every E epochs"""
    d, e = args.lr_decay
    lr = args.lr * (d ** -(epoch // e))
    for param_group in opt.param_groups:
        param_group['lr'] = lr


def selective_zero(s, new, forward=True):
    if new.any():  # if at least one video changed
        b = new.nonzero().squeeze(1)  # get the list of indices
        if forward:  # no state forward, no grad backward
            if isinstance(s[0], list):  # recurrent G
                for layer in range(len(s[0])):  # for every layer having a state
                    s[0][layer] = s[0][layer].index_fill(0, V(b), 0)  # mask state, zero selected indices
                for layer in range(len(s[1])):  # for every layer having a state
                    s[1][layer] = s[1][layer].index_fill(0, V(b), 0)  # mask state, zero selected indices
            else:  # simple convolutional G
                for layer in range(len(s)):  # for every layer having a state
                    s[layer] = s[layer].index_fill(0, V(b), 0)  # mask state, zero selected indices
        else:  # just no grad backward
            if isinstance(s[0], list):  # recurrent G
                for layer in range(len(s[0])):  # for every layer having a state
                    s[0][layer].register_hook(lambda g: g.index_fill(0, V(b), 0))  # zero selected gradients
                for layer in range(len(s[1])):  # for every layer having a state
                    s[1][layer].register_hook(lambda g: g.index_fill(0, V(b), 0))  # zero selected gradients
            else:  # simple convolutional G
                for layer in range(len(s)):  # for every layer having a state
                    s[layer].register_hook(lambda g: g.index_fill(0, V(b), 0))  # zero selected gradients


def selective_match(x_hat, x, new):
    if new.any():  # if at least one video changed
        b = new.nonzero().squeeze(1)  # get the list of indices
        for bb in b: x_hat[bb].copy_(x[bb])  # force the output to be the expected output


def selective_cross_entropy(logits, y, new, loss, count):
    if not new.any():
        return V(logits.data.new(1).zero_())  # returns a variable, so we don't care about what happened here
    b = new.nonzero().squeeze(1)  # get the list of indices
    count['ce_count'] += len(b)
    return loss(logits.index_select(0, V(b)), y.index_select(0, V(b)))  # performs loss for selected indices only


def train(train_loader, model, loss_fun, optimiser, epoch):
    print('Training epoch', epoch + 1)
    model.train()  # set model in train mode
    total_loss = {'mse': 0, 'ce': 0, 'ce_count': 0, 'per_ce': 0, 'rpl': 0}
    mse, nll_final, nll_periodic = loss_fun

    def compute_loss(x_, next_x, y_, state_, periodic=False):
        nonlocal previous_mismatch  # write access to variables of the enclosing function
        if args.mode == 'MatchNet':
            if not periodic and state_: selective_zero(state_, mismatch, forward=False)  # no grad to the past
        (x_hat, state_), (_, idx) = model(V(x_), state_)
        selective_zero(state_, mismatch)  # no state to the future, no grad from the future
        selective_match(x_hat.data, next_x, mismatch + previous_mismatch)  # last frame or first frame
        previous_mismatch = mismatch  # last frame <- first frame
        mse_loss_ = mse(x_hat, V(next_x))
        total_loss['mse'] += mse_loss_.data[0]
        ce_loss_ = selective_cross_entropy(idx, V(y_), mismatch, nll_final, total_loss)
        total_loss['ce'] += ce_loss_.data[0]
        if periodic:
            ce_loss_ = (ce_loss_, nll_periodic(idx, V(y_)))
            total_loss['per_ce'] += ce_loss_[1].data[0]
        total_loss['rpl'] += mse(x_hat, V(x_, volatile=True)).data[0]
        return ce_loss_, mse_loss_, state_, x_hat.data

    data_time = 0
    batch_time = 0
    end_time = time.time()
    state = None  # reset state at the beginning of a new epoch
    from_past = None  # performs only T - 1 steps for the first temporal batch
    previous_mismatch = torch.ByteTensor(args.batch_size).fill_(1)  # ignore first prediction
    if args.cuda: previous_mismatch = previous_mismatch.cuda()
    for batch_nb, (x, y) in enumerate(train_loader):
        data_time += time.time() - end_time
        if args.cuda:
            x = x.cuda(async=True)
            y = y.cuda(async=True)
        state = repackage_state(state)
        loss = 0
        # BTT loop
        if args.mode == 'MatchNet':
            if from_past:
                mismatch = y[0] != from_past[1]
                ce_loss, mse_loss, state, _ = compute_loss(from_past[0], x[0], from_past[1], state, periodic=True)
                loss += mse_loss * args.mu + ce_loss[0] * args.tau + ce_loss[1] * args.pi
            for t in range(0, min(args.big_t, x.size(0)) - 1):  # first batch we go only T - 1 steps forward / backward
                mismatch = y[t + 1] != y[t]
                ce_loss, mse_loss, state, x_hat_data = compute_loss(x[t], x[t + 1], y[t], state)
                loss += mse_loss * args.mu + ce_loss * args.tau
        elif args.mode == 'TempoNet':
            if from_past:
                mismatch = y[0] != from_past[1]
                ce_loss, mse_loss, state, _ = compute_loss(from_past[0], x[0], from_past[1], state)
                loss += mse_loss * args.mu + ce_loss * args.tau
            for t in range(0, min(args.big_t, x.size(0)) - 1):  # first batch we go only T - 1 steps forward / backward
                mismatch = y[t + 1] != y[t]
                last = t == min(args.big_t, x.size(0)) - 2
                ce_loss, mse_loss, state, x_hat_data = compute_loss(x[t], x[t + 1], y[t], state, periodic=last)
                if not last:
                    loss += mse_loss * args.mu + ce_loss * args.tau
                else:
                    loss += mse_loss * args.mu + ce_loss[0] * args.tau + ce_loss[1] * args.pi

        # compute gradient and do SGD step
        model.zero_grad()
        loss.backward()
        optimiser.step()

        # save last column for future
        from_past = x[-1], y[-1]

        # measure batch time
        batch_time += time.time() - end_time
        end_time = time.time()  # for computing data_time

        if (batch_nb + 1) % args.log_interval == 0:
            if args.view:
                for f in args.view:
                    show_four(x[t][f], x[t + 1][f], x_hat_data[f], f + 1)
                    if args.show_x_hat: show_ten(x[t][f], x_hat_data[f])
            total_loss['mse'] /= args.log_interval * args.big_t
            total_loss['rpl'] /= args.log_interval * args.big_t
            total_loss['per_ce'] /= args.log_interval
            if total_loss['ce_count']: total_loss['ce'] /= total_loss['ce_count']
            avg_batch_time = batch_time * 1e3 / args.log_interval
            avg_data_time = data_time * 1e3 / args.log_interval
            lr = optimiser.param_groups[0]['lr']  # assumes len(param_groups) == 1
            print('| epoch {:3d} | {:4d}/{:4d} batches | lr {:.3f} |'
                  ' ms/batch {:7.2f} | ms/data {:7.2f} | mMSE {:.2e} | CE {:.2e} | rpl mMSE {:.2e} | per CE {:.2e} |'.
                  format(epoch + 1, batch_nb + 1, len(train_loader), lr, avg_batch_time, avg_data_time,
                         total_loss['mse'] * 1e3, total_loss['ce'], total_loss['rpl'] * 1e3, total_loss['per_ce']))
            for k in total_loss: total_loss[k] = 0  # zero the losses
            batch_time = 0
            data_time = 0


def validate(val_loader, model, loss_fun):
    model.eval()  # set model in evaluation mode
    total_loss = {'mse': 0, 'ce': 0, 'ce_count': 0, 'per_ce': 0, 'rpl': 0}
    mse, nll_final, nll_periodic = loss_fun
    batches = enumerate(val_loader)

    _, (x, y) = next(batches)
    if args.cuda:
        x = x.cuda(async=True)
        y = y.cuda(async=True)
    previous_mismatch = y[0].byte().fill_(1)  # ignore first prediction
    state = None  # reset state at the beginning of a new epoch
    for batch_nb, (next_x, next_y) in batches:
        if args.cuda:
            next_x = next_x.cuda(async=True)
            next_y = next_y.cuda(async=True)
        mismatch = next_y[0] != y[0]
        (x_hat, state), (_, idx) = model(V(x[0], volatile=True), state)  # do not compute graph (volatile)
        selective_zero(state, mismatch)  # no state to the future
        selective_match(x_hat.data, next_x[0], mismatch + previous_mismatch)  # last frame or first frame
        previous_mismatch = mismatch  # last frame <- first frame
        total_loss['mse'] += mse(x_hat, V(next_x[0])).data[0]
        ce_loss = selective_cross_entropy(idx, V(y[0]), mismatch, nll_final, total_loss)
        total_loss['ce'] += ce_loss.data[0]
        if batch_nb % args.big_t == 0: total_loss['per_ce'] += nll_periodic(idx, V(y[0])).data[0]
        total_loss['rpl'] += mse(x_hat, V(x[0])).data[0]
        x, y = next_x, next_y

    total_loss['mse'] /= len(val_loader)  # average out
    total_loss['rpl'] /= len(val_loader)  # average out
    total_loss['per_ce'] /= len(val_loader) / args.big_t  # average out
    total_loss['ce'] /= total_loss['ce_count']  # average out
    return total_loss


def repackage_state(h):
    """
    Wraps hidden states in new Variables, to detach them from their history.
    """
    if not h:
        return None
    elif type(h) == V:
        return V(h.data)
    else:
        return list(repackage_state(v) for v in h)


if __name__ == '__main__':
    main()

__author__ = "Alfredo Canziani"
__credits__ = ["Alfredo Canziani"]
__maintainer__ = "Alfredo Canziani"
__email__ = "alfredo.canziani@gmail.com"
__status__ = "Production"  # "Prototype", "Development", or "Production"
__date__ = "Feb 17"
