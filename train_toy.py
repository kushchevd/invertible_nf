
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import argparse
import os
import time
import math
import numpy as np

import torch

import lib.optimizers as optim
import lib.layers.base as base_layers
import lib.layers as layers
from  lib.toy_data  import inf_train_gen
import lib.utils as utils
from lib.visualize_flow import visualize_transform

ACTIVATION_FNS = {
    'relu': torch.nn.ReLU,
    'tanh': torch.nn.Tanh,
    'elu': torch.nn.ELU,
    'selu': torch.nn.SELU,
    'fullsort': base_layers.FullSort,
    'maxmin': base_layers.MaxMin,
    'swish': base_layers.Swish,
    'LeakyLSwish': base_layers.LeakyLSwish,
    'CLipSwish': base_layers.CLipSwish,
    'lcube': base_layers.LipschitzCube,
}


parser = argparse.ArgumentParser()
parser.add_argument(
    '--data', choices=['swissroll', '8gaussians', 'pinwheel', 'circles', 'moons', '2spirals', 'checkerboard', 'rings'],
    type=str, default='pinwheel'
)
parser.add_argument('--coeff', type=float, default=0.9)
parser.add_argument('--vnorms', type=str, default='222222')
parser.add_argument('--n-lipschitz-iters', type=int, default=5)
parser.add_argument('--atol', type=float, default=None)
parser.add_argument('--rtol', type=float, default=None)
parser.add_argument('--learn-p', type=eval, choices=[True, False], default=False)
parser.add_argument('--mixed', type=eval, choices=[True, False], default=True)
parser.add_argument('--logdet', type=eval, choices=[True, False], default=True)

# DenseNet
parser.add_argument('--densenet', type=eval, choices=[True, False], default=True)
parser.add_argument('--densenet_depth', type=int, default=3)
parser.add_argument('--densenet_growth', type=int, default=16)
parser.add_argument('--learnable_concat', type=eval, choices=[True, False], default=True)
parser.add_argument('--start_learnable_concat', type=int, default=25000)
parser.add_argument('--lip_coeff', help='Lipschitz coeff for DenseNet', type=float, default=0.98)

parser.add_argument('--dims', type=str, default='128-128-128-128')
parser.add_argument('--act', type=str, choices=ACTIVATION_FNS.keys(), default='swish')
parser.add_argument('--nblocks', type=int, default=100)
parser.add_argument('--brute-force', type=eval, choices=[True, False], default=False)
parser.add_argument('--actnorm', type=eval, choices=[True, False], default=False)
parser.add_argument('--batchnorm', type=eval, choices=[True, False], default=False)
parser.add_argument('--exact-trace', type=eval, choices=[True, False], default=False)
parser.add_argument('--n-power-series', type=int, default=None)
parser.add_argument('--n-samples', type=int, default=1)
parser.add_argument('--n-dist', choices=['geometric', 'poisson'], default='geometric')

parser.add_argument('--niters', type=int, default=800)
parser.add_argument('--batch_size', type=int, default=500)
parser.add_argument('--test_batch_size', type=int, default=10000)
parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--weight-decay', type=float, default=1e-5)
parser.add_argument('--annealing-iters', type=int, default=0)

parser.add_argument('--save', type=str, default='experiments/iresnet_toy')
parser.add_argument('--viz_freq', type=int, default=100)
parser.add_argument('--val_freq', type=int, default=100)
parser.add_argument('--log_freq', type=int, default=10)
parser.add_argument('--gpu', type=int, default=0)
parser.add_argument('--seed', type=int, default=0)
args = parser.parse_args()


# logger
utils.makedirs(args.save)
logger = utils.get_logger(logpath=os.path.join(args.save, 'logs'), filepath=os.path.abspath(__file__))
logger.info(args)

device = torch.device('cuda:' + str(args.gpu) if torch.cuda.is_available() else 'cpu')

np.random.seed(args.seed)
torch.manual_seed(args.seed)
if device.type == 'cuda':
    torch.cuda.manual_seed(args.seed)

if args.act == 'CLipSwish':
    assert args.densenet_growth % 2 == 0, "Select an even densenet growth size!"



def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def standard_normal_sample(size):
    return torch.randn(size)


def standard_normal_logprob(z):
    logZ = -0.5 * math.log(2 * math.pi)
    return logZ - z.pow(2) / 2


def compute_loss(args, model, batch_size=None, beta=1.):
    if batch_size is None: batch_size = args.batch_size

    # load data
    x = inf_train_gen(args.data, batch_size=batch_size)
    x = torch.from_numpy(x).type(torch.float32).to(device)
    zero = torch.zeros(x.shape[0], 1).to(x)

    # transform to z
    z, delta_logp = model(x, zero)

    # compute log p(z)
    logpz = standard_normal_logprob(z).sum(1, keepdim=True)

    logpx = logpz - beta * delta_logp
    loss = -torch.mean(logpx)
    return loss, torch.mean(logpz), torch.mean(-delta_logp)


def compute_loss_wo_logdet(args, model, batch_size=None, beta=1.):
    if batch_size is None: batch_size = args.batch_size

    # load data
    x = inf_train_gen(args.data, batch_size=batch_size)
    x = torch.from_numpy(x).type(torch.float32).to(device)
    zero = torch.zeros(x.shape[0], 1).to(x)

    # transform to z
    z, delta_logp = model(x, zero)

    # compute log p(z)
    logpz = standard_normal_logprob(z).sum(1, keepdim=True)

    logpx = logpz #- beta * delta_logp
    loss = -torch.mean(logpx)
    return loss, torch.mean(logpz), torch.mean(-delta_logp)


def parse_vnorms():
    ps = []
    for p in args.vnorms:
        if p == 'f':
            ps.append(float('inf'))
        else:
            ps.append(float(p))
    return ps[:-1], ps[1:]


def compute_p_grads(model):
    scales = 0.
    nlayers = 0
    for m in model.modules():
        if isinstance(m, base_layers.InducedNormConv2d) or isinstance(m, base_layers.InducedNormLinear):
            scales = scales + m.compute_one_iter()
            nlayers += 1
    scales.mul(1 / nlayers).mul(0.01).backward()
    for m in model.modules():
        if isinstance(m, base_layers.InducedNormConv2d) or isinstance(m, base_layers.InducedNormLinear):
            if m.domain.grad is not None and torch.isnan(m.domain.grad):
                m.domain.grad = None


def build_nnet_densenet(activation_fn, input_channels, densenet_growth, densenet_depth, learnable_concat, lip_coeff):
    nnet = []
    total_in_channels = input_channels

    for i in range(densenet_depth):
        part_net = []

        # Change growth size for CLipSwish:
        if args.act == 'CLipSwish':
            output_channels = densenet_growth // 2
        else:
            output_channels = densenet_growth

        part_net.append(
            base_layers.get_linear(
                total_in_channels,
                output_channels,
                coeff=args.coeff,
                n_iterations=args.n_lipschitz_iters,
                atol=args.atol,
                rtol=args.rtol,
                domain=2,
                codomain=2,
                zero_init=False,
            )
        )

        part_net.append(activation_fn())

        nnet.append(
            layers.LipschitzDenseLayer(
                torch.nn.Sequential(*part_net),
                learnable_concat,
                lip_coeff
            )
        )

        total_in_channels += densenet_growth

    nnet.append(
        base_layers.get_linear(
            total_in_channels,
            input_channels,
            coeff=args.coeff,
            n_iterations=args.n_lipschitz_iters,
            atol=args.atol,
            rtol=args.rtol,
            domain=2,
            codomain=2,
            zero_init=False,
        )
    )
    return torch.nn.Sequential(*nnet)


def build_nnet(densenet, dims, activation_fn=torch.nn.ReLU):
    nnet = []
    domains, codomains = parse_vnorms()
    if args.learn_p:
        if args.mixed:
            domains = [torch.nn.Parameter(torch.tensor(0.)) for _ in domains]
        else:
            domains = [torch.nn.Parameter(torch.tensor(0.))] * len(domains)
        codomains = domains[1:] + [domains[0]]

    if densenet:
        return build_nnet_densenet(activation_fn, input_channels=dims[0], densenet_growth=args.densenet_growth,
                                   densenet_depth=args.densenet_depth, learnable_concat=args.learnable_concat,
                                   lip_coeff=args.lip_coeff)

    for i, (in_dim, out_dim, domain, codomain) in enumerate(zip(dims[:-1], dims[1:], domains, codomains)):
        nnet.append(activation_fn())
        nnet.append(
            base_layers.get_linear(
                in_dim,
                out_dim,
                coeff=args.coeff,
                n_iterations=args.n_lipschitz_iters,
                atol=args.atol,
                rtol=args.rtol,
                domain=domain,
                codomain=codomain,
                zero_init=(out_dim == 2),
            )
        )
    return torch.nn.Sequential(*nnet)


def reset_parameters(model):
    for m in model.modules():
        if isinstance(m, layers.LipschitzDenseLayer):
            torch.nn.init.ones_(m.K1_unnormalized)
            torch.nn.init.ones_(m.K2_unnormalized)


def update_lipschitz(model, n_iterations):
    for m in model.modules():
        if isinstance(m, base_layers.SpectralNormConv2d) or isinstance(m, base_layers.SpectralNormLinear):
            m.compute_weight(update=True, n_iterations=n_iterations)
        if isinstance(m, base_layers.InducedNormConv2d) or isinstance(m, base_layers.InducedNormLinear):
            m.compute_weight(update=True, n_iterations=n_iterations)


def get_learnable_params(model):
    concat_eta1 = []
    concat_eta2 = []
    concat_K1 = []
    concat_K2 = []
    for m in model.modules():
        if isinstance(m, layers.LipschitzDenseLayer):
            eta1_normalized, eta2_normalized = m.get_eta1_eta2()
            concat_eta1.append(eta1_normalized.item())
            concat_eta2.append(eta2_normalized.item())

            K1_unnormalized = m.K1_unnormalized
            K2_unnormalized = m.K2_unnormalized
            concat_K1.append(K1_unnormalized.item())
            concat_K2.append(K2_unnormalized.item())
    return concat_eta1, concat_eta2, concat_K1, concat_K2


def get_activation_params(model):
    alphas = []
    betas = []
    for m in model.modules():
        if isinstance(m, layers.base.activations.LeakyLSwish):
            alpha = m.alpha
            beta = m.beta
            alphas.append(round(alpha.item(), 2))
            betas.append(round(beta.item(), 2))
    return alphas, betas


def get_ords(model):
    ords = []
    for m in model.modules():
        if isinstance(m, base_layers.InducedNormConv2d) or isinstance(m, base_layers.InducedNormLinear):
            domain, codomain = m.compute_domain_codomain()
            if torch.is_tensor(domain):
                domain = domain.item()
            if torch.is_tensor(codomain):
                codomain = codomain.item()
            ords.append(domain)
            ords.append(codomain)
    return ords


def pretty_repr(a):
    return '[[' + ','.join(list(map(lambda i: f'{i:.2f}', a))) + ']]'



if __name__ == '__main__':
    activation_fn = ACTIVATION_FNS[args.act]
    build_densenet = args.densenet

    dims = [2] + list(map(int, args.dims.split('-'))) + [2]
    blocks = []
    if args.actnorm: blocks.append(layers.ActNorm1d(2))
    for _ in range(args.nblocks):
        blocks.append(
            layers.iResBlock(
                build_nnet(build_densenet, dims, activation_fn),
                n_dist=args.n_dist,
                n_power_series=args.n_power_series,
                exact_trace=args.exact_trace,
                brute_force=args.brute_force,
                n_samples=args.n_samples,
                neumann_grad=False,
                grad_in_forward=False,
            )
        )
        if args.actnorm: blocks.append(layers.ActNorm1d(2))
        if args.batchnorm: blocks.append(layers.MovingBatchNorm1d(2))
    model = layers.SequentialFlow(blocks).to(device)

    

    logger.info(model)
    logger.info("Number of trainable parameters: {}".format(count_parameters(model)))

    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    time_meter = utils.RunningAverageMeter(0.93)
    loss_meter = utils.RunningAverageMeter(0.93)
    logpz_meter = utils.RunningAverageMeter(0.93)
    delta_logp_meter = utils.RunningAverageMeter(0.93)

    end = time.time()
    best_loss = float('inf')
    model.train()
    for itr in range(1, args.niters + 1):
        optimizer.zero_grad()

        beta = min(1, itr / args.annealing_iters) if args.annealing_iters > 0 else 1.
        loss, logpz, delta_logp = compute_loss(args, model, beta=beta)
        loss_meter.update(loss.item())
        logpz_meter.update(logpz.item())
        delta_logp_meter.update(delta_logp.item())
        loss.backward()
        if args.learn_p and itr > args.annealing_iters: compute_p_grads(model)
        optimizer.step()

        # Start learning concat after X epochs
        if args.learnable_concat and (itr < args.start_learnable_concat):
            reset_parameters(model)

        update_lipschitz(model, args.n_lipschitz_iters)

        time_meter.update(time.time() - end)

        logger.info(
            'Iter {:04d} | Time {:.4f}({:.4f}) | Loss {:.6f}({:.6f})'
            ' | Logp(z) {:.6f}({:.6f}) | DeltaLogp {:.6f}({:.6f})'.format(
                itr, time_meter.val, time_meter.avg, loss_meter.val, loss_meter.avg, logpz_meter.val, logpz_meter.avg,
                delta_logp_meter.val, delta_logp_meter.avg
            )
        )

        if itr % args.val_freq == 0 or itr == args.niters:
            update_lipschitz(model, 200)
            with torch.no_grad():
                model.eval()
                
                if args.logdet:
                    test_loss, test_logpz, test_delta_logp = compute_loss(args, model, batch_size=args.test_batch_size)
                else: 
                    test_loss, test_logpz, test_delta_logp = compute_loss_wo_logdet(args, model, batch_size=args.test_batch_size)
                
                log_message = (
                    '[TEST] Iter {:04d} | Test Loss {:.6f} '
                )

                logger.info(log_message)
                logger.info('Ords: {}'.format(pretty_repr(get_ords(model))))

                if args.act == 'LeakyLSwish' and (itr % (args.val_freq * 5) == 0 or itr == args.niters):
                    alphas, betas = get_activation_params(model)
                    logger.info('alphas: {}'.format(pretty_repr(alphas)))
                    logger.info('betas: {}'.format(pretty_repr(betas)))

                if args.learnable_concat:
                    concat_eta1, concat_eta2, concat_K1, concat_K2 = get_learnable_params(model)
                    logger.info('eta1: {}'.format(pretty_repr(concat_eta1)))
                    logger.info('eta2: {}'.format(pretty_repr(concat_eta2)))
                    logger.info('K1: {}'.format(pretty_repr(concat_K1)))
                    logger.info('K2: {}'.format(pretty_repr(concat_K2)))

                if test_loss.item() < best_loss:
                    best_loss = test_loss.item()
                    utils.makedirs(args.save)
                    torch.save({
                        'args': args,
                        'state_dict': model.state_dict(),
                    }, os.path.join(args.save, 'checkpt.pth'))
                model.train()

        if itr == 1 or itr % args.viz_freq == 0:
            with torch.no_grad():
                model.eval()
                p_samples = inf_train_gen(args.data, batch_size=20000)

                sample_fn, density_fn = model.inverse, model.forward

                plt.figure(figsize=(9, 3))
                visualize_transform(
                    p_samples, torch.randn, standard_normal_logprob, transform=sample_fn, inverse_transform=density_fn,
                    samples=True, npts=400, device=device
                )
                if args.logdet:    
                    fig_filename = os.path.join(args.save, 'figs', '{:04d}.png'.format(itr))
                else:
                    fig_filename = os.path.join(args.save, 'figs_wo_logdet', '{:04d}.png'.format(itr))
                utils.makedirs(os.path.dirname(fig_filename))
                plt.savefig(fig_filename)
                plt.close()
                model.train()

        end = time.time()
     
    logger.info('Training has finished.')
