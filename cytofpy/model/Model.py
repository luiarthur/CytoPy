import copy
import math

import torch

from torch.distributions.log_normal import LogNormal
from torch.distributions import Normal, Gamma, Dirichlet, Beta, Bernoulli, Uniform
from torch.nn import ParameterList

from cytofpy.model.model_param import ModelParam, VI
from cytofpy.model.vae import VAE

import numpy as np

# Set default type to float64 (instead of float32)
torch.set_default_dtype(torch.float64)

@torch.jit.script
def logit(p):
    return torch.log(p) - torch.log1p(-p)

def compute_Z(v, H, tau):
    """
    This enables the backward gradient computations via Z.
    Notice at the end, we basically return Z (binary tensor).
    But we make use of the smoothed Z, which is differentiable.
    We detach (Z - smoothed_Z) so that the gradients are not 
    computed, and then add back smoothed_Z for the return.
    The only gradient will then be that of smoothed Z.
    """
    x = logit(v[None, :]) - logit(H)
    smoothed_Z = (x / tau).sigmoid()
    Z = (v > H).double()
    return (Z - smoothed_Z).detach() + smoothed_Z

@torch.jit.script
def prob_miss(y, b0, b1, b2):
    return (b0 + b1 * y + b2 * y**2).sigmoid()

def solve_beta(y, p):
    k = len(p)
    p = np.array(p)
    Y = np.concatenate([[np.ones(k)], [y], [y**2]]).T
    beta = np.linalg.solve(Y, np.log(p) - np.log1p(-p))
    return torch.tensor(beta).double()

# TODO: Put this test in tests/
# solve_beta(np.array([-5.0, -3.0, -1.0]), np.array([.05, .8 , .05]))
# exact: -8.35785565, -6.49610001, -1.08268334

def gen_beta_est(yi, y_quantiles, p_bounds):
    yi_neg = yi[yi < 0]
    y_bounds = np.percentile(yi_neg.numpy(), y_quantiles)
    return solve_beta(y_bounds, p_bounds)

# TODO: Put this test in tests/
# gen_beta_est(torch.randn(10000), [3, 30, 50], [.05, .8, .05])
# approx: -18.763069081151038 -30.50605414798636 -10.654946017898444

def default_priors(y, K:int=30, L=None,
                   y_quantiles=[0, 35, 70], p_bounds=[.05, .8, .05], y_bounds=None):
    I = len(y)

    J = y[0].size(1)
    for i in range(I):
        assert(y[i].size(1) == J)

    N = [y[i].size(0) for i in range(I)]

    K = K

    if L is None:
        L = [5, 5]
    
    b0 = torch.zeros(I)
    b1 = torch.zeros(I)
    b2 = torch.zeros(I)

    for i in range(I):
        if y_bounds is None:
            beta = gen_beta_est(y[i].flatten(), y_quantiles, p_bounds)
        else:
            beta = solve_beta(np.array(y_bounds), p_bounds)

        b0[i] = beta[0]
        b1[i] = beta[1]
        b2[i] = beta[2]


    return {'I': I, 'J': J, 'N': N, 'L': L, 'K': K,
            #
            'delta0': Gamma(1, 1),
            'delta1': Gamma(1, 1),
            #
            'sig2': LogNormal(-1, .1),
            #
            'eta0': Dirichlet(torch.ones(L[0]) / L[0]),
            'eta1': Dirichlet(torch.ones(L[1]) / L[1]),
            #
            'alpha': Gamma(.1, .1),
            'H': Uniform(0, 1),
            #
            'b0': b0,
            'b1': b1,
            'b2': b2,
            #
            'noisy_var': 10.0,
            #
            'W': Dirichlet(torch.ones(K) / K),
            #
            'eps': Beta(1, 99)
            }

class Model(VI):
    def __init__(self, y, priors, m=None, y_mean_init=-3.0, y_sd_init=0.1,
                 tau=0.1, verbose=1, use_stick_break=True, model_noisy=True, scale=1):

        self.model_noisy = model_noisy
        self.verbose = verbose

        if self.verbose >= 0:
            print('use_stick_break: {}'.format(use_stick_break))
            print('tau: {}'.format(tau))
            print('model_noisy: {}'.format(model_noisy))

        # Use stick breaking construction of IBP
        self.use_stick_break = use_stick_break

        # Dimensions of data
        self.I = priors['I']
        self.J = priors['J']
        self.N = priors['N']
        self.Nsum = sum(self.N)
        self.scale = scale

        # Tuning Parameters
        self.tau = tau
        # coefficients defining the missing mechanism
        self.b0 = priors['b0']
        self.b1 = priors['b1']
        self.b2 = priors['b2']

        # Dimensions of parameters
        self.L = priors['L']
        self.K = priors['K']

        # Noisy variance
        self.noisy_sd = torch.sqrt(torch.tensor(priors['noisy_var']))

        # Store priors
        self.priors = priors

        # Register m
        if m is None:
            self.m = [torch.isnan(yi) for yi in y]
        else:
            self.m = m

        # register y
        self.y_data = y

        # register log_qy
        self.log_qy = None

        self.msum = [mi.sum() for mi in self.m]

        ### Assign Model Parameters###
        self.mp = {}
        self.mp['delta0'] = ModelParam(self.L[0], 'positive',
                                       m=torch.ones(self.L[0]), s=torch.ones(self.L[0]))
        self.mp['delta1'] = ModelParam(self.L[1], 'positive',
                                       m=torch.ones(self.L[1]), s=torch.ones(self.L[1]))

        if self.model_noisy:
            self.mp['eps'] = ModelParam(self.I, 'unit_interval',
                                        m=torch.ones(self.I) * priors['eps'].mean,
                                        s=torch.ones(self.I) * .001)

        self.mp['sig2'] = ModelParam(self.I, 'positive',
                                     m=torch.ones(self.I) * -1.0,
                                     s=torch.ones(self.I) * .1)

        self.mp['eta0'] = ModelParam((self.I, self.J, self.L[0] - 1), 'simplex')
        self.mp['eta1'] = ModelParam((self.I, self.J, self.L[1] - 1), 'simplex')
        self.mp['W'] = ModelParam((self.I, self.K - 1), 'simplex')
        self.mp['alpha'] = ModelParam(1, 'positive')
        if self.use_stick_break:
            self.mp['v'] = ModelParam(self.K, 'unit_interval',
                                      torch.ones(self.K) * .99)
        else:
            self.mp['v'] = ModelParam(self.K, 'unit_interval')
        self.mp['H'] = ModelParam((self.J, self.K), 'unit_interval')
        ### END OF Assign Model Parameters###

        # This must be done after assigning model parameters
        super(Model, self).__init__()
        # self.y_vp = ParameterList(mp_yi.vp for mp_yi in self.mp['y'])

        self.y_vae = [VAE(self.J, mean_init=y_mean_init, sd_init=y_sd_init)
                      for i in range(self.I)]

        vp_list = []
        for i in range(self.I):
            x = self.y_vae[i].parameters()
            for xi in x:
                vp_list.append(xi)

        self.y_vae_vp = ParameterList(vp_list)
        
    def loglike(self, params, idx):
        y = params['y']
        sig = params['sig2'].sqrt()

        ll = 0.0
        for i in range(self.I):
            mi = self.m[i][idx[i], :]

            # Y: Ni x J
            # muz: Lz
            # etaz_i: 1 x J x Lz

            # Ni x J x Lz
            mu0 = -params['delta0'].cumsum(0)
            d0 = Normal(mu0[None, None, :], sig[i]).log_prob(y[i][:, :, None])
            d0 += params['eta0'][i:i+1, :, :].log()

            mu1 = params['delta1'].cumsum(0)
            d1 = Normal(mu1[None, None, :], sig[i]).log_prob(y[i][:, :, None])
            d1 += params['eta1'][i:i+1, :, :].log()
            
            # Ni x J
            logmix_L0 = torch.logsumexp(d0, 2)
            logmix_L1 = torch.logsumexp(d1, 2)

            # p: K
            if self.use_stick_break:
                v = params['v'].cumprod(0)
            else:
                v = params['v']

            # Z: J x K
            # H: J x K
            Z = compute_Z(v, params['H'], self.tau)

            # Z_mix: Ni x J x K
            Z_mix = Z[None, :] * logmix_L1[:, :, None] + (1 - Z[None, :]) * logmix_L0[:, :, None]

            # Z_mix_sum: Ni x K
            Z_mix_sum = Z_mix.sum(1)

            # f: Ni x J x K
            f = Z_mix_sum + params['W'][i:i+1, :].log()

            # Ni-dim
            lli = torch.logsumexp(f, 1)

            fac = self.N[i] / y[i].size(0)

            if self.model_noisy:
                eps_i = params['eps'][i]
                # eps_i = torch.tensor(1e-6)
                lli_quiet = lli + torch.log1p(-eps_i)
                lli_noisy = Normal(0, self.noisy_sd).log_prob(y[i]).sum(1) + eps_i.log()
                lli = torch.stack([lli_quiet, lli_noisy]).logsumexp(0).sum(0)
            else:
                lli = lli.sum(0)

            # p(m | y, theta)
            if mi.sum() > 0:
                pm_i = prob_miss(y[i],
                                 self.b0[i],
                                 self.b1[i],
                                 self.b2[i])
                lli += pm_i[mi].log().sum()
            
            ll += lli * fac

        return ll

    def log_q(self, reals, idx):
        lq = 0.0
        for key in reals:
            if key == 'y':
                lq += self.log_qy
            else:
                lq += self.mp[key].log_q(reals[key])
        return lq 

    def log_prior(self, reals, params, idx):
        lp = 0.0
        for key in reals:
            if key == 'v':
                if self.use_stick_break:
                    tmp = Beta(params['alpha'], 1).log_prob(params['v'])
                else:
                    tmp = Beta(params['alpha'] / self.K, 1).log_prob(params['v'])
                tmp += self.mp['v'].logabsdetJ(reals['v'], params['v'])
                lp += tmp.sum()
            elif key != 'y':
                tmp = self.priors[key].log_prob(params[key])
                tmp += self.mp[key].logabsdetJ(reals[key], params[key])
                lp += tmp.sum()

        return lp

    def sample_reals(self, idx):
        reals = {}
        for key in self.mp:
            if key != 'y':
                reals[key] = self.mp[key].real_sample()

        reals['y'] = []
        log_qy = 0.0
        for i in range(self.I):
            yi_dat = self.y_data[i][idx[i], :]
            mi = self.m[i][idx[i], :]
            yi, log_qyi = self.y_vae[i](yi_dat, mi, self.N[i], scale=self.scale)
            reals['y'].append(yi)
            log_qy += log_qyi
        self.log_qy = log_qy

        return reals

    def transform(self, reals):
        params = {}
        for key in self.mp:
            params[key] = self.mp[key].transform(reals[key])
        params['y'] = reals['y']

        return params

    def sample_params(self, idx):
        """
        used for post processing
        """
        reals = self.sample_reals(idx)
        return self.transform(reals)

    def forward(self, idx):
        reals = self.sample_reals(idx)
        params = self.transform(reals)
        ll = self.loglike(params, idx)
        lp = self.log_prior(reals, params, idx)
        lq = self.log_q(reals, idx)
        elbo = ll + lp - lq
        return elbo, ll.item(), lp.item(), lq.item()
