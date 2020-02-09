"""
Created on Tue Jan 29 14:38:26 2019
@author: mzaiss

"""
experiment_id = 'exA08_gradientecho_freq_phaae_enc_FFT2'
sequence_class = "gre_dream"
experiment_description = """
GRE or 1 D imaging
"""
excercise = """
this file starts from solA06 and combines A06 and A07.
We want now to have both frequency and phase encoding simultaneously,
to be able to create the 2D - fourier transform of the signal,
to get a 2D image with full encoding.
this file starts from solA06. 
A08.1  set NRep=12, as in A07, create a loop over NRep to generate  the fourier transform for every rep. Check if it is always identical.
A08.2. in addition to the frequency encoding, add phase encoding gradient events as in A07.
A08.3. to have full encoding you now have to fourier transform a second time, once loop over NReps iFFT in szread, then loop over szread iFFT i NRep dimension 
A08.4. plot the result as an image, plt.imshow()  or plt.imshow(np.abs(space), interpolation='none',aspect = sz[0]/szread)
A08.5. find the correct fft shift for display using np.roll(..) or np.fft.ifftshift / np.fft.fftshift
A08.6. The two fourier transforms can be concatenated using np.fft.ifft2. 
A08.7. set NReps =sz[1], set szread=sz[0], then increase sz to e.g. 24,24 
A08.8. change phantom to 2D phantom, or brain 
A08.9. Use plt.imshow() to show the magnitude image (np.abs), and the phase image (np.angle), 
        if phase has wraps or  chekckerboard->fftshift
        use np.flip(matrix,(0,1)) and np.transpose(matrix) to get wanted orientation

Now you have your first fully encoded MR image!!!

"""
#%%
#matplotlib.pyplot.close(fig=None)
#%%
import os, sys
import numpy as np
import scipy
import scipy.io
from  scipy import ndimage
import torch
import cv2
import matplotlib.pyplot as plt
from torch import optim
import core.spins
import core.scanner
import core.nnreco
import core.opt_helper
import core.target_seq_holder
import core.FID_normscan
import warnings
import matplotlib.cbook
warnings.filterwarnings("ignore",category=matplotlib.cbook.mplDeprecation)

from importlib import reload
reload(core.scanner)

double_precision = False
do_scanner_query = False

use_gpu = 1
gpu_dev = 0

if sys.platform != 'linux':
    use_gpu = 0
    gpu_dev = 0
print(experiment_id)    
print('use_gpu = ' +str(use_gpu)) 

# NRMSE error function
def e(gt,x):
    return 100*np.linalg.norm((gt-x).ravel())/np.linalg.norm(gt.ravel())
    
# torch to numpy
def tonumpy(x):
    return x.detach().cpu().numpy()

# get magnitude image
def magimg(x):
    return np.sqrt(np.sum(np.abs(x)**2,2))

def phaseimg(x):
    return np.angle(1j*x[:,:,1]+x[:,:,0])

def magimg_torch(x):
  return torch.sqrt(torch.sum(torch.abs(x)**2,1))

def tomag_torch(x):
    return torch.sqrt(torch.sum(torch.abs(x)**2,-1))

# device setter
def setdevice(x):
    if double_precision:
        x = x.double()
    else:
        x = x.float()
    if use_gpu:
        x = x.cuda(gpu_dev)    
    return x 

#############################################################################
## S0: define image and simulation settings::: #####################################
sz = np.array([12,12])                      # image size
extraMeas = 1                               # number of measurmenets/ separate scans
NRep = extraMeas*sz[1]                      # number of total repetitions
NRep = 1                                 # number of total repetitions
szread=128
T = szread + 5 + 2                               # number of events F/R/P
NSpins = 16**2                               # number of spin sims in each voxel
NCoils = 1                                  # number of receive coil elements
noise_std = 0*1e-3                          # additive Gaussian noise std
kill_transverse = False                     #
import time; today_datestr = time.strftime('%y%m%d')
NVox = sz[0]*szread

#############################################################################
## S1: Init spin system and phantom::: #####################################
# initialize scanned object
spins = core.spins.SpinSystem(sz,NVox,NSpins,use_gpu+gpu_dev,double_precision=double_precision)

real_phantom_resized = np.zeros((sz[0],sz[1],5), dtype=np.float32)
real_phantom_resized[6,6,:]=np.array([1.0, 1, 0.1, 0,0])
real_phantom_resized[2,3,:]=np.array([0.5,    1, 0.1, 0,0])

## load phantom from file
#cutoff = 1e-12
##real_phantom = scipy.io.loadmat('../../data/phantom2D.mat')['phantom_2D']
#real_phantom = scipy.io.loadmat('../../data/numerical_brain_cropped.mat')['cropped_brain']
#
#real_phantom_resized = np.zeros((sz[0],sz[1],5), dtype=np.float32)
#for i in range(5):
#    t = cv2.resize(real_phantom[:,:,i], dsize=(sz[0],sz[1]), interpolation=cv2.INTER_CUBIC)
#    if i == 0:
#        t[t < 0] = 0
#    elif i == 1 or i == 2:
#        t[t < cutoff] = cutoff        
#    real_phantom_resized[:,:,i] = t
    
spins.set_system(real_phantom_resized)

if 1:
    plt.figure("""phantom""")
    param=['PD','T1','T2','dB0','rB1']
    for i in range(5):
        plt.subplot(151+i), plt.title(param[i])
        ax=plt.imshow(real_phantom_resized[:,:,i], interpolation='none')
        fig = plt.gcf()
        fig.colorbar(ax) 
    fig.set_size_inches(18, 3)
    plt.show()
   
#begin nspins with R2* = 1/T2*
R2star = 30.0
omega = np.linspace(0,1,NSpins) - 0.5   # cutoff might bee needed for opt.
omega = np.expand_dims(omega[:],1).repeat(NVox, axis=1)
omega*=0.99 # cutoff large freqs
omega = R2star * np.tan ( np.pi  * omega)
spins.omega = torch.from_numpy(omega.reshape([NSpins,NVox])).float()
spins.omega = setdevice(spins.omega)
## end of S1: Init spin system and phantom ::: #####################################


#############################################################################
## S2: Init scanner system ::: #####################################
scanner = core.scanner.Scanner_fast(sz,NVox,NSpins,NRep,T,NCoils,noise_std,use_gpu+gpu_dev,double_precision=double_precision)

B1plus = torch.zeros((scanner.NCoils,1,scanner.NVox,1,1), dtype=torch.float32)
B1plus[:,0,:,0,0] = torch.from_numpy(real_phantom_resized[:,:,4].reshape([scanner.NCoils, scanner.NVox]))
B1plus[B1plus == 0] = 1    # set b1+ to one, where we dont have phantom measurements
B1plus[:] = 1
scanner.B1plus = setdevice(B1plus)

#############################################################################
## S3: MR sequence definition ::: #####################################
# begin sequence definition
# allow for extra events (pulses, relaxation and spoiling) in the first five and last two events (after last readout event)
adc_mask = torch.from_numpy(np.ones((T,1))).float()
adc_mask[:5]  = 0
adc_mask[-2:] = 0
scanner.set_adc_mask(adc_mask=setdevice(adc_mask))

# RF events: rf_event and phases
rf_event = torch.zeros((T,NRep,2), dtype=torch.float32)
rf_event[3,:,0] = 90*np.pi/180  # 90deg excitation now for every rep
rf_event = setdevice(rf_event)
scanner.init_flip_tensor_holder()    
scanner.set_flip_tensor_withB1plus(rf_event)
# rotate ADC according to excitation phase
rfsign = ((rf_event[3,:,0]) < 0).float()
scanner.set_ADC_rot_tensor(-rf_event[3,0,1] + np.pi/2 + np.pi*rfsign) #GRE/FID specific

# event timing vector 
event_time = torch.from_numpy(0.08*1e-3*np.ones((scanner.T,scanner.NRep))).float()
event_time[:,0] =  0.08*1e-3
event_time[-1,:] =  5
event_time = setdevice(event_time)

# gradient-driver precession
# Cartesian encoding
gradm_event = torch.zeros((T,NRep,2), dtype=torch.float32)
gradm_event[4,:,1] = -0.5*szread
gradm_event[5:-2,:,1] = 1
gradm_event = setdevice(gradm_event)

scanner.init_gradient_tensor_holder()
scanner.set_gradient_precession_tensor(gradm_event,sequence_class)  # refocusing=False for GRE/FID, adjust for higher echoes
## end S3: MR sequence definition ::: #####################################



#############################################################################
## S4: MR simulation forward process ::: #####################################
scanner.init_signal()
scanner.forward_fast(spins, event_time)
  
fig=plt.figure("""seq and signal"""); fig.set_size_inches(64, 7)
plt.subplot(311); plt.title('seq: RF, time, ADC')
plt.plot(np.tile(tonumpy(adc_mask),NRep).flatten('F'),'.',label='ADC')
plt.plot(tonumpy(event_time).flatten('F'),'.',label='time')
plt.plot(tonumpy(rf_event[:,:,0]).flatten('F'),label='RF')
plt.legend()
plt.subplot(312); plt.title('seq: gradients')
plt.plot(tonumpy(gradm_event[:,:,0]).flatten('F'),label='gx')
plt.plot(tonumpy(gradm_event[:,:,1]).flatten('F'),label='gy')
plt.legend()
plt.subplot(313); plt.title('signal')
plt.plot(tonumpy(scanner.signal[0,:,:,0,0]).flatten('F'),label='real')
plt.plot(tonumpy(scanner.signal[0,:,:,1,0]).flatten('F'),label='imag')
plt.legend()
plt.show()

#%% ############################################################################
## S5: MR reconstruction of signal ::: #####################################
fig=plt.figure("""Fourier Transform""")
plt.subplot(311)
spectrum = tonumpy(scanner.signal[0,adc_mask.flatten()!=0,:,:2,0].clone()) 
spectrum = spectrum[:,:,0]+spectrum[:,:,1]*1j # get all ADC signals as complex numpy array
plt.plot(np.real(spectrum).flatten('F'),label='real')
plt.plot(np.imag(spectrum).flatten('F'),label='imag')
major_ticks = np.arange(0, szread*NRep, szread) # this adds ticks at the correct position szread
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()

space = np.zeros_like(spectrum)

spectrum = np.roll(spectrum,szread//2,axis=0)
spectrum = np.roll(spectrum,NRep//2,axis=1)

for i in range(0,NRep):
    space[:,i] = np.fft.ifft(spectrum[:,i])

# fftshift
space= np.roll(space,szread//2-1,axis=0)
space = np.roll(space,NRep//2-1,axis=1)
plt.subplot(312)
plt.plot(np.abs(space.flatten('F')))
plt.plot(np.imag(space.flatten('F')))
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
            
plt.subplot(337)
plt.imshow(real_phantom_resized[:,:,0], interpolation='none')

        
plt.subplot(338)
plt.imshow(np.abs(space), interpolation='none',aspect = sz[0]/szread)
plt.subplot(339)
plt.imshow(np.angle(space)*(np.abs(space)>0.2*np.max(np.abs(space))), interpolation='none',aspect = sz[0]/szread)

plt.show()                     
        