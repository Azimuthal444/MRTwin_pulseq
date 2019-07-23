import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as fnn
from termcolor import colored
import matplotlib.pyplot as plt
from torch import optim
import os, sys
import scipy
import math
from torch.optim.optimizer import Optimizer
import socket

from sys import platform
import time
from shutil import copyfile
from core.pulseq_exporter import pulseq_write_GRE
from core.pulseq_exporter import pulseq_write_RARE
from core.pulseq_exporter import pulseq_write_BSSFP
from core.pulseq_exporter import pulseq_write_EPI

if sys.version_info[0] < 3:
    import cPickle as pickle
else:
    import pickle
    
# NRMSE error function
def e(gt,x):
    return 100*np.linalg.norm((gt-x).ravel())/np.linalg.norm(gt.ravel())

# torch to numpy
def tonumpy(x):
    return x.detach().clone().cpu().numpy()
    
def get_cuda_mem_GB():
    return torch.cuda.get_device_properties(0).total_memory / 1024.0**3

def magimg(x):
    return np.sqrt(np.sum(np.abs(x)**2,2))

def phaseimg(x):
    return np.angle(1j*x[:,:,1]+x[:,:,0])

# Adam variation to allow for blocking gradient steps on individual entries of parameter vector
class Bdam(Optimizer):
    r"""Implements Adam algorithm (with tricks).

    It has been proposed in `Adam: A Method for Stochastic Optimization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False)

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                 weight_decay=0, amsgrad=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay, amsgrad=amsgrad)
        super(Bdam, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(Bdam, self).__setstate__(state)
        for group in self.param_groups:
            group.setdefault('amsgrad', False)

    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data
                
                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse gradients, please consider SparseAdam instead')
                amsgrad = group['amsgrad']

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p.data)
                    if amsgrad:
                        # Maintains max of all exp. moving avg. of sq. grad. values
                        state['max_exp_avg_sq'] = torch.zeros_like(p.data)

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                if amsgrad:
                    max_exp_avg_sq = state['max_exp_avg_sq']
                beta1, beta2 = group['betas']

                state['step'] += 1

                if group['weight_decay'] != 0:
                    grad.add_(group['weight_decay'], p.data)

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(1 - beta1, grad)
                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = max_exp_avg_sq.sqrt().add_(group['eps'])
                else:
                    denom = exp_avg_sq.sqrt().add_(group['eps'])

                bias_correction1 = 1 - beta1 ** state['step']
                bias_correction2 = 1 - beta2 ** state['step']
                step_size = group['lr'] * math.sqrt(bias_correction2) / bias_correction1

                # set to zero masked-out entries
                if hasattr(p,'zero_grad_mask'):
                    exp_avg *= p.zero_grad_mask
                
                p.data.addcdiv_(-step_size, exp_avg, denom)

        return loss
    
class SGD_vanilla(Optimizer):
    r"""Implements (vanilla) stochastic gradient descent
    """

    def __init__(self, params, lr=0.1):
        defaults = dict(lr=lr)
        super(SGD_vanilla, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(SGD_vanilla, self).__setstate__(state)

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                d_p = p.grad.data

                # set to zero masked-out entries
                if hasattr(p,'zero_grad_mask'):
                    d_p *= p.zero_grad_mask                
                
                p.data.add_(-group['lr'], d_p)

        return loss

# optimization helper class
class OPT_helper():
    def __init__(self,scanner,spins,NN,nmb_total_samples_dataset):
        self.globepoch = 0
        self.subjidx = 0
        self.nmb_total_samples_dataset = nmb_total_samples_dataset
        
        self.learning_rate = 0.02
        self.custom_learning_rate = None   # allow for differerent learning rates in different parameter groups
        
        self.opti_mode = 'seq'
        self.optimzer_type = 'Adam'
        
        self.scanner = scanner
        self.spins = spins
        self.NN = NN
        
        self.optimizer = None
        self.best_optimizer_state = None
        self.init_variables = None
        self.phi_FRP_model = None
        self.reparameterize = None
        
        self.scanner_opt_params = None
        self.opt_param_idx = []
        
        self.last_reco = None
        self.last_error = None        
        
        self.param_reco_history = []
        
        self.target_seq_holder = None
        
        self.aux_params = None
        
    def set_handles(self,init_variables,phi_FRP_model,reparameterize=None):
        self.init_variables = init_variables
        self.phi_FRP_model = phi_FRP_model
        self.reparameterize = reparameterize
        
    def set_opt_param_idx(self,opt_param_idx):
        self.opt_param_idx  = opt_param_idx
        
    def new_batch(self):
        self.globepoch += 1
        
        if hasattr(self.spins, 'batch_size'):
            batch_size = self.spins.batch_size
        else:
            batch_size = 1
        
        self.subjidx = np.random.choice(self.nmb_total_samples_dataset, batch_size, replace=False)
        
    # evaluate loss and partial derivatives over parameters
    def weak_closure(self):
        kumloss = 0
        bsz = 1
        
        self.optimizer.zero_grad()
        
        if hasattr(self, 'batch_size'):
            bsz = self.batch_size
            
        for i in range(bsz):
            loss,last_reco,last_error = self.phi_FRP_model(self.scanner_opt_params, None)
            self.last_reco = last_reco
            self.last_error = last_error
            loss.backward()
            
            kumloss += loss
        
        return kumloss 
            
    def init_optimizer(self):
        WEIGHT_DECAY = 1e-8
        
        # only optimize a subset of params
        optimizable_params = []
        for i in range(len(self.opt_param_idx)):
            if self.custom_learning_rate == None:
                optimizable_params.append(self.scanner_opt_params[self.opt_param_idx[i]] )
            else:
                optimizable_params.append({'params':self.scanner_opt_params[self.opt_param_idx[i]], 'lr': self.custom_learning_rate[self.opt_param_idx[i]]} )
            
        # optimize only sequence parameters
        if self.opti_mode == 'seq':
            if self.optimzer_type == 'Adam':
                self.optimizer = Bdam(optimizable_params, lr=self.learning_rate, weight_decay=WEIGHT_DECAY)
            elif self.optimzer_type == 'SGD_vanilla':
                self.optimizer = SGD_vanilla(optimizable_params, lr=self.learning_rate)
            else:
                self.optimizer = optim.LBFGS(optimizable_params, lr=self.learning_rate)
                
        # optimize only NN reconstruction module parameters
        elif self.opti_mode == 'nn':
            if self.optimzer_type == 'Adam':
                self.optimizer = Bdam(list(self.NN.parameters()), lr=self.learning_rate, weight_decay=WEIGHT_DECAY)
            else:
                self.optimizer = optim.LBFGS(list(self.NN.parameters()), lr=self.learning_rate)
            
        # optimize both sequence and NN reconstruction module parameters
        elif self.opti_mode == 'seqnn':
            optimizable_params.append({'params':self.NN.parameters(), 'lr': self.learning_rate} )
            
            if self.optimzer_type == 'Adam':
                self.optimizer = Bdam(optimizable_params, lr=self.learning_rate, weight_decay=WEIGHT_DECAY)
            else:
                self.optimizer = optim.LBFGS(optimizable_params, lr=self.learning_rate)
            
        
    # main training function
    def train_model(self, training_iter = 100, show_par=False, do_vis_image=False, save_intermediary_results=False, query_scanner=False, query_kwargs=None):
        
        for i in range(len(self.scanner_opt_params)):
            if i in self.opt_param_idx:
                self.scanner_opt_params[i].requires_grad = True
            else:
                self.scanner_opt_params[i].requires_grad = False
        
        self.init_optimizer()
        
        # continue optimization if optimizer state is saved
        if self.best_optimizer_state is not None:
            checkpoint = torch.load("results/optimizer_state.tmp")
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            
            print('Loading saved optimizer state....')
            
        # main optimization loop
        for inner_iter in range(training_iter):
            
            # evaluate initial image state before doing optimizer step
            if inner_iter == 0:
                _,self.last_reco,self.last_error = self.phi_FRP_model(self.scanner_opt_params, None)
            print(colored("\033[93m iter %d, recon error = %f \033[0m%%" % (inner_iter,self.last_error), 'green'))
            
            self.print_status(do_vis_image,self.last_reco)
            
            if query_scanner:
                experiment_id, today_datestr, sequence_class = query_kwargs
                
                sim_sig = self.scanner.signal.clone()
                
                # do real scanner reco
                self.export_to_pulseq(experiment_id,today_datestr,sequence_class)
                self.scanner.send_job_to_real_system(experiment_id,today_datestr,jobtype="lastiter")
                self.scanner.get_signal_from_real_system(experiment_id,today_datestr,jobtype="lastiter")
                
                self.scanner.adjoint()
                meas_sig = self.scanner.signal.clone()
                meas_reco = self.scanner.reco.clone()                
                
                plt.subplot(141)
                meas_reco = tonumpy(meas_reco.detach()).reshape([self.scanner.sz[0],self.scanner.sz[1],2])
                ax = plt.imshow(magimg(meas_reco), interpolation='none')
                meas_target = tonumpy(self.target_seq_holder.meas_reco).reshape([self.scanner.sz[0],self.scanner.sz[1],2])
                mag_meas_target = magimg(meas_target)
                plt.clim(np.min(mag_meas_target),np.max(mag_meas_target))
                
                fig = plt.gcf()
                fig.colorbar(ax)
                plt.title("meas mag ADJ")
                
                plt.subplot(142)
                ax = plt.imshow(phaseimg(meas_reco), interpolation='none')
                fig = plt.gcf()
                fig.colorbar(ax)
                plt.title("meas phase ADJ")
                
                NCol = self.scanner.NCol
                NRep = self.scanner.NRep                
                
                coil_idx = 0
                adc_idx = np.where(self.scanner.adc_mask.cpu().numpy())[0]
                sim_kspace = sim_sig[coil_idx,adc_idx,:,:2,0]
                sim_kspace = magimg(tonumpy(sim_kspace.detach()).reshape([NCol,NRep,2]))
                
                plt.subplot(143)
                plt.imshow(sim_kspace, interpolation='none')
                plt.title("sim kspace")                
                
                meas_kspace = meas_sig[coil_idx,adc_idx,:,:2,0]
                meas_kspace = magimg(tonumpy(meas_kspace.detach()).reshape([NCol,NRep,2]))     
                
                plt.subplot(144)
                plt.imshow(meas_kspace, interpolation='none')
                plt.title("meas kspace")                   

                fig.set_size_inches(18, 3)
                
                plt.ion()
                plt.show()
                
                meas_error = e(meas_target.ravel(),meas_reco.ravel())    
                print(colored("\033[93m iter %d, REAL MEAS error = %f \033[0m%%" % (inner_iter,meas_error), 'green'))            
            
            # save entire history of optimized params/reco images
            if save_intermediary_results:
                tosave_opt_params = self.scanner_opt_params
                
                # i.e. non-cartesian trajectiries, any custom reparameterization
                if self.reparameterize is not None:
                    tosave_opt_params = self.reparameterize(tosave_opt_params)
                
                saved_state = dict()
                saved_state['adc_mask'] = tonumpy(tosave_opt_params[0])
                saved_state['flips_angles'] = tonumpy(tosave_opt_params[1])
                saved_state['event_times'] = tonumpy(tosave_opt_params[2])
                saved_state['grad_moms'] = tonumpy(tosave_opt_params[3].clone())
                saved_state['grad_moms'] = tonumpy(tosave_opt_params[3].clone())
                saved_state['kloc'] = tonumpy(self.scanner.kspace_loc.clone())
                saved_state['learn_rates'] = self.custom_learning_rate
                
                legs=['x','y','z']
                for i in range(3):
                    M_roi = tonumpy(self.scanner.ROI_signal[:,:,1+i]).transpose([1,0]).reshape([(self.scanner.T)*self.scanner.NRep])
                    saved_state['ROI_def %d, %s'  % (self.scanner.ROI_def,legs[i])]  = M_roi

                saved_state['reco_image'] = tonumpy(self.last_reco.clone())
                saved_state['signal'] = tonumpy(self.scanner.signal)
                saved_state['error'] = self.last_error
                
                if query_scanner:
                    saved_state['meas_signal'] = tonumpy(meas_sig)
                    saved_state['meas_adj_reco'] = meas_reco
                
                self.param_reco_history.append(saved_state)      
                
            #self.new_batch()
            self.optimizer.step(self.weak_closure)
            
    def train_model_supervised(self, training_iter = 100, show_par=False, do_vis_image=False, save_intermediary_results=False):
        
        for i in range(len(self.scanner_opt_params)):
            if i in self.opt_param_idx:
                self.scanner_opt_params[i].requires_grad = True
            else:
                self.scanner_opt_params[i].requires_grad = False
        
        self.init_optimizer()
        
        # continue optimization if optimizer state is saved
        if self.best_optimizer_state is not None:
            checkpoint = torch.load("results/optimizer_state.tmp")
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            
            print('Loading saved optimizer state....')
            
        # main optimization loop
        for inner_iter in range(training_iter):
            
            # evaluate initial image state before doing optimizer step
            if inner_iter == 0:
                _,self.last_reco,self.last_error = self.phi_FRP_model(self.scanner_opt_params, None, do_test_onphantom=True)
            print(colored("\033[93m iter %d, recon error = %f \033[0m%%" % (inner_iter,self.last_error), 'green'))
            
            self.optimizer.step(self.weak_closure)            
            
            # save entire history of optimized params/reco images
            if save_intermediary_results and np.mod(inner_iter,10) == 0 and inner_iter > 0:
                _,self.last_reco,self.last_error = self.phi_FRP_model(self.scanner_opt_params, None, do_test_onphantom=True)
                self.print_status(do_vis_image,self.last_reco)
                print(colored("\033[93m phantom eval: iter %d, recon error = %f \033[0m%%" % (inner_iter,self.last_error), 'red'))
                
                tosave_opt_params = self.scanner_opt_params
                
                # i.e. non-cartesian trajectiries, any custom reparameterization
                if self.reparameterize is not None:
                    tosave_opt_params = self.reparameterize(tosave_opt_params)
                
                saved_state = dict()
                saved_state['adc_mask'] = tonumpy(tosave_opt_params[0])
                saved_state['flips_angles'] = tonumpy(tosave_opt_params[1])
                saved_state['event_times'] = tonumpy(tosave_opt_params[2])
                saved_state['grad_moms'] = tonumpy(tosave_opt_params[3].clone())
                saved_state['grad_moms'] = tonumpy(tosave_opt_params[3].clone())
                saved_state['kloc'] = tonumpy(self.scanner.kspace_loc.clone())
                saved_state['learn_rates'] = self.custom_learning_rate
                
                legs=['x','y','z']
                for i in range(3):
                    M_roi = tonumpy(self.scanner.ROI_signal[:,:,1+i]).transpose([1,0]).reshape([(self.scanner.T)*self.scanner.NRep])
                    saved_state['ROI_def %d, %s'  % (self.scanner.ROI_def,legs[i])]  = M_roi

                saved_state['reco_image'] = tonumpy(self.last_reco.clone())
                saved_state['signal'] = tonumpy(self.scanner.signal)
                saved_state['error'] = self.last_error
                
                self.param_reco_history.append(saved_state)      
                
    # main training function
    def train_model_with_restarts(self, nmb_rnd_restart=15, training_iter=10, show_par=False, do_vis_image=False, save_intermediary_results=False, query_scanner=False, query_kwargs=None):
        
        # init gradients and flip events
        best_error = 1000
        
        for outer_iter in range(nmb_rnd_restart):
            print('restarting the model training... ')

            self.scanner_opt_params = self.init_variables()
            self.init_optimizer()
           
            for i in range(len(self.scanner_opt_params)):
                if i in self.opt_param_idx:
                    self.scanner_opt_params[i].requires_grad = True
                else:
                    self.scanner_opt_params[i].requires_grad = False
            
            # main optimization loop
            for inner_iter in range(training_iter):
                
                # evaluate initial image state before doing optimizer step
                if inner_iter == 0:
                    _,self.last_reco,self.last_error = self.phi_FRP_model(self.scanner_opt_params, None)
                print(colored("\033[93m iter %d, recon error = %f \033[0m%%" % (inner_iter,self.last_error), 'green'))
                
                self.print_status(do_vis_image,self.last_reco)
                
                #self.new_batch()
                self.optimizer.step(self.weak_closure)
                
                if self.last_error < best_error:
                    print("recon error = %f" %self.last_error)
                    best_error = self.last_error
                    
                    best_vars = []
                    for par in self.scanner_opt_params:
                        best_vars.append(par.detach().clone())            
            
        for pidx in range(len(self.scanner_opt_params)):
            self.scanner_opt_params[pidx] = best_vars[pidx]
            self.scanner_opt_params[pidx].requires_grad = True        # needed?              
                
    def set_target(self,target):
        self.target = target
            
    def print_status(self, do_vis_image=False, reco=None):
        if do_vis_image:
            sz=self.spins.sz
            
            if hasattr(self.spins,'batch_size'):
                recoimg = tonumpy(reco[0,:,:]).reshape([sz[0],sz[1],2])
                PD0_mask = self.spins.PD0_mask[0,:,:]
            else:
                recoimg = tonumpy(reco).reshape([sz[0],sz[1],2])
                PD0_mask = self.spins.PD0_mask
                
            # clear previous figure stack            
            plt.clf()            
            
            ax1=plt.subplot(251)
            ax=plt.imshow(magimg(self.target), interpolation='none')
            plt.clim(np.min(np.abs(self.target)),np.max(np.abs(self.target)))
            #plt.clim(0,1)
            fig = plt.gcf()
            fig.colorbar(ax)
            plt.title('target')
            plt.ion()
            
            # show phase only for complex-valued targets
            if len(self.target.shape) > 2 and self.target.shape[2] > 1:
                ax1=plt.subplot(256)
                ax=plt.imshow(tonumpy(PD0_mask)*phaseimg(self.target), interpolation='none')
                plt.clim(-np.pi,np.pi) 
                #plt.clim(0,1)
                fig = plt.gcf()
                fig.colorbar(ax)
                plt.title('target phase')
                plt.ion()
            
            plt.subplot(252, sharex=ax1, sharey=ax1)
            ax=plt.imshow(magimg(recoimg), interpolation='none')
            plt.clim(np.min(np.abs(self.target)),np.max(np.abs(self.target)))
            fig = plt.gcf()
            fig.colorbar(ax)
            plt.title('reco')
            plt.ion()
            
            plt.subplot(257, sharex=ax1, sharey=ax1)
            ax=plt.imshow(tonumpy(PD0_mask)*phaseimg(recoimg), interpolation='none')
            plt.clim(-np.pi,np.pi) 
            fig = plt.gcf()
            fig.colorbar(ax)
            plt.title('reco phase')
            plt.ion()
            
            todisplay_opt_params = self.scanner_opt_params
            
            # i.e. non-cartesian trajectiries, any custom reparameterization
            if self.reparameterize is not None:
                todisplay_opt_params =self.reparameterize(todisplay_opt_params)            
            
            if todisplay_opt_params[1].dim() == 3:
                FA=todisplay_opt_params[1][:,:,0]
                phi=todisplay_opt_params[1][:,:,1]
            else:
                FA=todisplay_opt_params[1]
                phi=todisplay_opt_params[1][:,:,1]
            plt.subplot(253)
            ax=plt.imshow(tonumpy(FA.permute([1,0]))*180/np.pi,cmap=plt.get_cmap('nipy_spectral'))
            plt.ion()
            plt.title('FA [\N{DEGREE SIGN}]')
            plt.clim(-180,270)
            fig = plt.gcf()
            fig.colorbar(ax)
            fig.set_size_inches(18, 3)
            
            plt.subplot(258)
            ax=plt.imshow(tonumpy(phi.permute([1,0]))*180/np.pi,cmap=plt.get_cmap('nipy_spectral'))
            plt.ion()
            plt.title('phase [\N{DEGREE SIGN}]')
            plt.clim(-180,270)
            fig = plt.gcf()
            fig.colorbar(ax)
            fig.set_size_inches(18, 3)
            
            
            plt.subplot(154)
            ax=plt.imshow(tonumpy(torch.abs(todisplay_opt_params[2]).permute([1,0])),cmap=plt.get_cmap('nipy_spectral'))
            plt.ion()
            plt.title('TR [s]')
            fig = plt.gcf()
            fig.set_size_inches(18, 3)
            fig.colorbar(ax)
              
# grad x grad z plot - old          
#            ax1=plt.subplot(2, 5, 5)
#            ax=plt.imshow(tonumpy(todisplay_opt_params[3][:,:,0].permute([1,0])),cmap=plt.get_cmap('nipy_spectral'))
#            plt.ion()
#            plt.title('gradx')
#            fig = plt.gcf()
#            fig.set_size_inches(18, 3)
#            fig.colorbar(ax)
#
# 			 ax1=plt.subplot(2, 5, 10)
#            ax=plt.imshow(tonumpy(todisplay_opt_params[3][:,:,1].permute([1,0])),cmap=plt.get_cmap('nipy_spectral'))
#            plt.ion()
#            plt.title('grady')
#            fig = plt.gcf()
#            fig.set_size_inches(18, 3)
#            fig.colorbar(ax)

# k-space plot             
            ax1=plt.subplot(155)
            kx= tonumpy(self.scanner.kspace_loc[:,:,0])
            ky= tonumpy(self.scanner.kspace_loc[:,:,1])
            for i in range(kx.shape[1]):
                plt.plot(kx[:,i],ky[:,i])
                
            fig.set_size_inches(18, 3)            
            
            plt.show()
            plt.pause(0.02)
            
            
            legs=['x','y','z']
            for i in range(3):
                plt.subplot(1, 3, i+1)
                plt.plot(tonumpy(self.scanner.ROI_signal[:,:,1+i]).transpose([1,0]).reshape([(self.scanner.T)*self.scanner.NRep]) )
                if (i==0) and (self.target_seq_holder is not None):
                    plt.plot(tonumpy(self.target_seq_holder.ROI_signal[:,:,1]).transpose([1,0]).reshape([(self.scanner.T)*self.scanner.NRep]) ) 
                if (i==2):
                    plt.plot(tonumpy(self.scanner.ROI_signal[:,:,4]).transpose([1,0]).reshape([(self.scanner.T)*self.scanner.NRep]),'--') 
                plt.title("ROI_def %d, %s" % (self.scanner.ROI_def,legs[i]))
                fig = plt.gcf()
                fig.set_size_inches(16, 3)
            plt.show()
            plt.pause(0.02)
            
    # save current optimized parameter state to matlab array
    def export_to_matlab(self, experiment_id, today_datestr):
        _,reco,error = self.phi_FRP_model(self.scanner_opt_params, None)
        
        tosave_opt_params = self.scanner_opt_params
        
        # i.e. non-cartesian trajectiries, any custom reparameterization
        if self.reparameterize is not None:
            tosave_opt_params =self.reparameterize(tosave_opt_params)
        
        scanner_dict = dict()
        scanner_dict['adc_mask'] = tonumpy(self.scanner.adc_mask)
        scanner_dict['B1'] = tonumpy(self.scanner.B1)
        scanner_dict['flips'] = tonumpy(tosave_opt_params[1])
        scanner_dict['event_times'] = np.abs(tonumpy(tosave_opt_params[2]))
        scanner_dict['grad_moms'] = tonumpy(tosave_opt_params[3])
        scanner_dict['kloc'] = tonumpy(self.scanner.kspace_loc)
        scanner_dict['reco'] = tonumpy(reco).reshape([self.scanner.sz[0],self.scanner.sz[1],2])
        scanner_dict['ROI'] = tonumpy(self.scanner.ROI_signal)
        scanner_dict['sz'] = self.scanner.sz
        #scanner_dict['adjoint_mtx'] = tonumpy(self.scanner.G_adj.permute([2,3,0,1,4]))
        scanner_dict['signal'] = tonumpy(self.scanner.signal)
        
        basepath = self.get_base_path(experiment_id, today_datestr)
        
        try:
            os.makedirs(basepath)
            os.makedirs(os.path.join(basepath,"data"))
        except:
            print('export_to_matlab: directory already exists')
            pass        
            
        scipy.io.savemat(os.path.join(basepath,"scanner_dict.mat"), scanner_dict)
        
    def get_base_path(self, experiment_id, today_datestr):
        if platform == 'linux':
            hostname = socket.gethostname()
            if hostname == 'vaal' or hostname == 'madeira4' or hostname == 'gadgetron':
                basepath = '/media/upload3t/CEST_seq/pulseq_zero/sequences'
            else:                                                     # cluster
                basepath = 'out'
        else:
            basepath = 'K:\CEST_seq\pulseq_zero\sequences'

        basepath = os.path.join(basepath, "seq" + today_datestr)
        basepath = os.path.join(basepath, experiment_id)

        return basepath   
        
    def export_to_pulseq(self, experiment_id, today_datestr, sequence_class, plot_seq=False):
        basepath = self.get_base_path(experiment_id, today_datestr)
        
        fn_lastiter_array = "lastiter_arr.npy"
        fn_pulseq = "lastiter.seq"
        
        # overwrite protection (gets trigger if pulseq file already exists)
#        today_datetimestr = time.strftime("%y%m%d%H%M%S")
#        if os.path.isfile(os.path.join(basepath, fn_pulseq)):
#            try:
#                copyfile(os.path.join(basepath, fn_pulseq), os.path.join(basepath, fn_pulseq + ".bak." + today_datetimestr))    
#                copyfile(os.path.join(basepath, fn_lastiter_array), os.path.join(basepath, fn_lastiter_array + ".bak." + today_datetimestr))    
#            except:
#                pass
            
        _,reco,error = self.phi_FRP_model(self.scanner_opt_params, None)
        tosave_opt_params = self.scanner_opt_params
        
        # i.e. non-cartesian trajectiries, any custom reparameterization
        if self.reparameterize is not None:
            tosave_opt_params =self.reparameterize(tosave_opt_params)            
        
        flips_numpy = tonumpy(tosave_opt_params[1])
        event_time_numpy = tonumpy(tosave_opt_params[2])
        grad_moms_numpy = tonumpy(tosave_opt_params[3])
        
        # save lastiter seq param array
        lastiter_array = dict()
        lastiter_array['adc_mask'] = tonumpy(self.scanner.adc_mask)
        lastiter_array['B1'] = tonumpy(self.scanner.B1)
        lastiter_array['flips'] = flips_numpy
        lastiter_array['event_times'] = event_time_numpy
        lastiter_array['grad_moms'] = grad_moms_numpy
        lastiter_array['kloc'] = tonumpy(self.scanner.kspace_loc)
        lastiter_array['reco'] = tonumpy(reco).reshape([self.scanner.sz[0],self.scanner.sz[1],2])
        lastiter_array['ROI'] = tonumpy(self.scanner.ROI_signal)
        lastiter_array['sz'] = self.scanner.sz
        lastiter_array['signal'] = tonumpy(self.scanner.signal)
        lastiter_array['sequence_class'] = sequence_class
        
        try:
            os.makedirs(basepath)
            os.makedirs(os.path.join(basepath,"data"))
        except:
            pass
        np.save(os.path.join(os.path.join(basepath, fn_lastiter_array)), lastiter_array)
        
        # save sequence
        seq_params = flips_numpy, event_time_numpy, grad_moms_numpy
        
        if sequence_class.lower() == "gre":
            pulseq_write_GRE(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=plot_seq)
        elif sequence_class.lower() == "rare":
            pulseq_write_RARE(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=plot_seq)
        elif sequence_class.lower() == "bssfp":
            pulseq_write_BSSFP(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=plot_seq)
        elif sequence_class.lower() == "epi":
            pulseq_write_EPI(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=plot_seq)
        
    # save entire history of the optimized parameters
    def save_param_reco_history(self, experiment_id, today_datestr, sequence_class, generate_pulseq=True):
        basepath = self.get_base_path(experiment_id, today_datestr)
        
        fn_alliter_array = "alliter_arr.npy"
        
        param_reco_history = self.param_reco_history
        
        NIter = len(param_reco_history)
        sz_x = self.scanner.sz[0]
        sz_y = self.scanner.sz[1]
        
        T = self.scanner.T
        NRep = self.scanner.NRep
        
        all_adc_masks = np.zeros((NIter,T))
        all_flips = np.zeros((NIter,T,NRep,2))
        all_event_times = np.zeros((NIter,T,NRep))
        all_grad_moms = np.zeros((NIter,T,NRep,2))
        all_kloc = np.zeros((NIter,T,NRep,2))
        all_reco_images = np.zeros((NIter,sz_x,sz_y,2))
        all_signals = np.zeros((NIter,self.scanner.NCoils,T,NRep,3))
        all_errors = np.zeros((NIter,1))
        
        for ni in range(NIter):
            all_adc_masks[ni] = param_reco_history[ni]['adc_mask'].ravel()
            all_flips[ni] = param_reco_history[ni]['flips_angles']
            all_event_times[ni] = param_reco_history[ni]['event_times']
            all_grad_moms[ni] = param_reco_history[ni]['grad_moms']
            all_kloc[ni] = param_reco_history[ni]['kloc']
            all_reco_images[ni] = param_reco_history[ni]['reco_image'].reshape([sz_x,sz_y,2])
            all_signals[ni] = param_reco_history[ni]['signal'].reshape([self.scanner.NCoils,T,NRep,3])
            all_errors[ni] = param_reco_history[ni]['error']
        
        alliter_dict = dict()
        alliter_dict['all_adc_masks'] = all_adc_masks
        alliter_dict['flips'] = all_flips
        alliter_dict['event_times'] = all_event_times
        alliter_dict['grad_moms'] = all_grad_moms
        alliter_dict['all_kloc'] = all_kloc
        alliter_dict['reco_images'] = all_reco_images
        alliter_dict['all_signals'] = all_signals
        alliter_dict['all_errors'] = all_errors
        alliter_dict['sz'] = np.array([sz_x,sz_y])
        alliter_dict['T'] = T
        alliter_dict['NRep'] = NRep
        alliter_dict['target'] = self.target
        alliter_dict['sequence_class'] = sequence_class
        alliter_dict['B1'] = tonumpy(self.scanner.B1)
        
        try:
            os.makedirs(basepath)
            os.makedirs(os.path.join(basepath,"data"))
        except:
            pass
        np.save(os.path.join(os.path.join(basepath, fn_alliter_array)), alliter_dict)
        
        # generate sequence files
        if generate_pulseq:
            for ni in range(NIter):
                fn_pulseq = "iter" + str(ni).zfill(6) + ".seq"
                
                flips_numpy = param_reco_history[ni]['flips_angles']
                event_time_numpy = param_reco_history[ni]['event_times']
                grad_moms_numpy = param_reco_history[ni]['grad_moms']    
                
                seq_params = flips_numpy, event_time_numpy, grad_moms_numpy
                
                if sequence_class.lower() == "gre":
                    pulseq_write_GRE(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=False)
                elif sequence_class.lower() == "rare":
                    pulseq_write_RARE(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=False)
                elif sequence_class.lower() == "bssfp":
                    pulseq_write_BSSFP(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=False)
                elif sequence_class.lower() == "epi":
                    pulseq_write_EPI(seq_params, os.path.join(basepath, fn_pulseq), plot_seq=False)          
        
        
    # save entire history of the optimized parameters (to Matlab)
    def save_param_reco_history_matlab(self, experiment_id, today_datestr):
        basepath = self.get_base_path(experiment_id, today_datestr)
        try:
            os.makedirs(basepath)
            os.makedirs(os.path.join(basepath,"data"))
        except:
            print('save_param_reco_history: directory already exists')
            pass
            
        param_reco_history = self.param_reco_history
        
        aux_info = dict()
        aux_info['sz'] = self.scanner.sz
        aux_info['NRep'] = self.scanner.NRep
        aux_info['T'] = self.scanner.T
        aux_info['target'] = self.target
        aux_info['ROI_def'] = self.scanner.ROI_def
        fpath=basepath+'/param_reco_history.pdb'
#        fpath=os.path.join(path,"param_reco_history.pdb")
        f = open(fpath, "wb")
        pickle.dump((param_reco_history, aux_info), f)
        f.close()
        
        NIter = len(param_reco_history)
        sz_x = self.scanner.sz[0]
        sz_y = self.scanner.sz[1]
        
        T = self.scanner.T
        NRep = self.scanner.NRep
        
        all_adc_masks = np.zeros((NIter,T))
        all_flips = np.zeros((NIter,T,NRep,2))
        all_event_times = np.zeros((NIter,T,NRep))
        all_grad_moms = np.zeros((NIter,T,NRep,2))
        all_kloc = np.zeros((NIter,T,NRep,2))
        all_reco_images = np.zeros((NIter,sz_x,sz_y,2))
        all_signals = np.zeros((NIter,T,NRep,3))
        all_errors = np.zeros((NIter,1))
        
        for ni in range(NIter):
            all_adc_masks[ni] = param_reco_history[ni]['adc_mask'].ravel()
            all_flips[ni] = param_reco_history[ni]['flips_angles']
            all_event_times[ni] = param_reco_history[ni]['event_times']
            all_grad_moms[ni] = param_reco_history[ni]['grad_moms']
            all_kloc[ni] = param_reco_history[ni]['kloc']
            all_reco_images[ni] = param_reco_history[ni]['reco_image'].reshape([sz_x,sz_y,2])
            all_signals[ni] = param_reco_history[ni]['signal'].reshape([T,NRep,3])
            all_errors[ni] = param_reco_history[ni]['error']
        
        scanner_dict = dict()
        scanner_dict['all_adc_masks'] = all_adc_masks
        scanner_dict['flips'] = all_flips
        scanner_dict['event_times'] = all_event_times
        scanner_dict['grad_moms'] = all_grad_moms
        scanner_dict['all_kloc'] = all_kloc
        scanner_dict['reco_images'] = all_reco_images
        scanner_dict['all_signals'] = all_signals
        scanner_dict['all_errors'] = all_errors
        scanner_dict['sz'] = np.array([sz_x,sz_y])
        scanner_dict['T'] = T
        scanner_dict['NRep'] = NRep
        scanner_dict['target'] = self.target
        
        scipy.io.savemat(os.path.join(basepath,"all_iter.mat"), scanner_dict)
        
    def query_cluster_job(self,query_kwargs):
        basepath = 'out'
        experiment_id, today_datestr, sequence_class = query_kwargs

        basepath = os.path.join(basepath, "seq" + today_datestr)
        basepath = os.path.join(basepath, experiment_id)
        
        fn_jobcontrol = "jobcontrol.txt"     
        fn_param_reco_history = "temp_param_reco_history.npy"
        fn_lastparam = "temp_lastparam.npy"   
        
        if os.path.exists(os.path.join(os.path.join(basepath, fn_jobcontrol))):
            with open(os.path.join(os.path.join(basepath, fn_jobcontrol)),"r") as f:
                job_lines = f.readlines()
                
            current_iteration = int(job_lines[1])
            if current_iteration > 0:
                self.param_reco_history = list(np.load(os.path.join(os.path.join(basepath, fn_param_reco_history)),allow_pickle=True))
                self.scanner_opt_params = np.load(os.path.join(os.path.join(basepath, fn_lastparam)),allow_pickle=True)                
            else:
                current_iteration = 0
                
        else:
            current_iteration = 0
            
        return current_iteration
                                   
    def update_cluster_job(self,query_kwargs,current_iteration,isfinished):
        basepath = 'out'
        experiment_id, today_datestr, sequence_class = query_kwargs

        basepath = os.path.join(basepath, "seq" + today_datestr)
        basepath = os.path.join(basepath, experiment_id)
        
        fn_jobcontrol = "jobcontrol.txt"     
        fn_param_reco_history = "temp_param_reco_history.npy"
        fn_lastparam = "temp_lastparam.npy"        
        
        if isfinished:
            job_lines = ['2\n',str(current_iteration+1)]
            
            os.remove(os.path.join(os.path.join(basepath, fn_param_reco_history)))
            os.remove(os.path.join(os.path.join(basepath, fn_lastparam)))
        else:
            param_reco_history = self.param_reco_history
            tosave_opt_params = self.scanner_opt_params
            
            try:
                os.makedirs(os.path.join(basepath,"data"))
            except:
                pass
            
            np.save(os.path.join(os.path.join(basepath, fn_param_reco_history)), param_reco_history)
            np.save(os.path.join(os.path.join(basepath, fn_lastparam)), tosave_opt_params)
            
            job_lines = ['0\n', str(current_iteration+1)]
            
        with open(os.path.join(os.path.join(basepath, fn_jobcontrol)),"w") as f:
            f.writelines(job_lines)
        
        
        
    