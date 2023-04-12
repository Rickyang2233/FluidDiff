import copy
import os
import sys
sys.path.append('..')
import argparse
import math
from tqdm import tqdm 

import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.optim import Adam, lr_scheduler

from data import NaiverStokes_Dataset
from diffuser import GaussianDiffusion
from unet import Unet3D, EMA
from physics_loss import vorticity_residual

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use('Agg')

parse = argparse.ArgumentParser(description='3D Denoising Diffusion Training')

parse.add_argument('--device-no', type=int, default=0, help='device to use (default 0)')
parse.add_argument('--ema-decay', type=float, default=0.995, help='ema decay rate, default 0.995')
parse.add_argument('--epoch-start-ema', type=int, default=2, help='epoch to start ema, default 2')
parse.add_argument('--train-batch-size', type=int, default=4, help='batch size for training, default 4')
parse.add_argument('--test-batch-size', type=int, default=1, help='batch size for testing, default 1')
parse.add_argument('--train-obj', type=str, choices=['pred_noise', 'pred_x0'], default='pred_x0', help='object for nn, default pred_noise')
parse.add_argument('--train-lr', type=float, default=1e-4, help='learning rate for training, default 1e-4')
parse.add_argument('--train-lrf', type=float, default=0.1, help='learning rate factor for training, default 0.1')
parse.add_argument('--train-epochs', type=int, default=100, help='number of epochs for training, default 100')
parse.add_argument('--resume-training', action='store_true', help='resume training')
parse.add_argument('--phyloss_weight', type=float, default=0.0, help='weight of physical loss, default 0.0')

def save_config(
        args: argparse.Namespace, 
        path: str):
    argsDict = args.__dict__
    with open(path+'/config.txt', 'w') as f:
        f.writelines('---------------------- Config ----------------------' + '\n')
        for key, value in argsDict.items():
            f.writelines(key + ' : ' + str(value) + '\n')
        f.writelines('----------------------------------------------------' + '\n')

def set_random_seed(
        seed: int,
        deterministic: bool = False,
        benchmark: bool = False):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    if deterministic:
        torch.backends.cudnn.deterministic = True
    if benchmark:
        torch.backends.cudnn.benchmark = True

class Trainer:
    def __init__(
            self,
            diffusion_model: GaussianDiffusion,
            *,
            device_no: int,
            ema_decay: float=0.995,
            epoch_start_ema: int=2,
            train_batch_size: int=4,
            test_batch_size: int=1,
            train_obj: str='pred_noise',
            train_lr: float=1e-4,
            train_lrf: float=0.1,
            train_epochs: int=100,
            resume_training: bool=False,
            phyloss_weight: float=0.1
            ):
        
        self.device = torch.device('cuda', device_no)
        self.diffusion_model = diffusion_model
        self.ema = EMA(ema_decay)
        self.ema_model = copy.deepcopy(self.diffusion_model)
        self.epoch_start_ema = epoch_start_ema
        self.phyloss_weight = phyloss_weight
        self.train_obj = train_obj

        self.tb_writer = SummaryWriter(log_dir=f'./logs/ns_V1e-5_T20_{phyloss_weight:.1f}phyloss_{train_obj}')

        self.train_ds = NaiverStokes_Dataset(data_dir='../../data/ns_data_T20_v1e-03_N1800.mat')
        self.test_ds = NaiverStokes_Dataset(data_dir='../../data/ns_data_T20_v1e-03_N200.mat')

        self.train_dl = DataLoader(self.train_ds, batch_size=train_batch_size, shuffle=True, num_workers=8)
        self.test_dl = DataLoader(self.test_ds, batch_size=test_batch_size, shuffle=False, num_workers=8)

        self.optimizer = Adam(self.diffusion_model.model.parameters(), lr=train_lr)
        lf = lambda x: ((1 + math.cos(x*math.pi/train_epochs)) / 2) * (1 - train_lrf) + train_lrf
        self.scheduler = lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lf)

        self.train_epochs = train_epochs
        self.epoch = 0
        if resume_training:
            self.load_checkpoint()
        
        self.reset_parameters()

        print("Trainer initialized, the network is objective to:", diffusion_model.objective)
    
    def reset_parameters(self):
        self.ema_model.model.load_state_dict(self.diffusion_model.model.state_dict())
    
    def step_ema(self):
        if self.epoch < self.epoch_start_ema:
            self.reset_parameters()
            return
        self.ema.update_model_average(self.ema_model.model, self.diffusion_model.model)
    
    def save_checkpoint(self):
        torch.save({
            'epoch': self.epoch,
            'model_state_dict': self.diffusion_model.model.state_dict(),
            'ema_model_state_dict': self.ema_model.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict(),
            }, f'./ckpts/ns_V1e-5_T20_{self.phyloss_weight}phyloss_{self.train_obj}/ckpt.pt')
        print("Checkpoint saved")
    
    def load_checkpoint(self, initialize_lr: bool=True):
        checkpoint = torch.load(f'./ckpts/ns_V1e-5_T20_{self.phyloss_weight}phyloss_{self.train_obj}/ckpt.pt')
        self.diffusion_model.model.load_state_dict(checkpoint['model_state_dict'])
        self.ema_model.model.load_state_dict(checkpoint['ema_model_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        if not initialize_lr:
            self.scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        self.epoch = checkpoint['epoch']
        print("Training resumed")

    def train(self):
        for epoch in range(self.train_epochs):

            self.diffusion_model.model.train()
            pbar_train = tqdm(self.train_dl, dynamic_ncols=True)
            cum_train_loss = 0
            for i, data in enumerate(pbar_train):
                x = data['x'].to(self.device)
                x_prev = data['x_prev'].to(self.device)
                x_next = data['x_next'].to(self.device)
                y = data['y'].to(self.device)
                x_pred, dn_loss = self.diffusion_model(x, cond=y)
                phy_loss = self.phyloss_weight * vorticity_residual(x_pred, x_prev, x_next, visc=1e-3, dt=1e-3) if self.phyloss_weight > 0 else torch.tensor(0., device=self.device)
                loss = dn_loss + self.phyloss_weight * phy_loss
                cum_train_loss += loss.item()
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                pbar_train.set_description(f'Train Epoch {epoch}/{self.train_epochs}, Avg Loss: {cum_train_loss / (i+1):.4f}')
                self.tb_writer.add_scalar('train/loss', loss.item(), epoch*len(self.train_dl)+i)
                self.tb_writer.add_scalar('train/dn_loss', dn_loss.item(), epoch*len(self.train_dl)+i)
                self.tb_writer.add_scalar('train/phy_loss', phy_loss.item(), epoch*len(self.train_dl)+i)

            self.scheduler.step()
            self.step_ema()

            self.diffusion_model.model.eval()
            pbar_test = tqdm(self.test_dl, dynamic_ncols=True)
            with torch.no_grad():
                cum_val_loss = 0
                for i, data in enumerate(pbar_test):
                    x = data['x'].to(self.device)
                    x_prev = data['x_prev'].to(self.device)
                    x_next = data['x_next'].to(self.device)
                    y = data['y'].to(self.device)
                    x_pred, dn_loss = self.diffusion_model(x, cond=y)
                    phy_loss = self.phyloss_weight * vorticity_residual(x_pred, x_prev, x_next, visc=1e-3, dt=1e-3) if self.phyloss_weight > 0 else torch.tensor(0., device=self.device)
                    loss = dn_loss + self.phyloss_weight * phy_loss
                    cum_val_loss += loss.item()
                    pbar_test.set_description(f'Val Epoch {epoch}/{self.train_epochs}, Loss: {cum_val_loss / (i+1):.4f}')
                    self.tb_writer.add_scalar('test/loss', loss.item(), epoch*len(self.test_dl)+i)
                
                if epoch % 5 == 0:
                    print(f"Sampling at epoch {epoch}")
                    x_pred = self.diffusion_model.sample(cond=y)
                    x_pred = x_pred.cpu().numpy().squeeze()
                    x = x.cpu().numpy().squeeze()
                    fig1, ax1 = plt.subplots(4, 5, figsize=(20, 16))
                    ax1 = ax1.flatten()
                    for i in range(20):
                        ax1[i].imshow(x[i], cmap='jet')
                        ax1[i].axis('off')
                    fig2, ax2 = plt.subplots(4, 5, figsize=(20, 16))
                    ax2 = ax2.flatten()
                    for i in range(20):
                        ax2[i].imshow(x_pred[i], cmap='jet')
                        ax2[i].axis('off')
                    self.tb_writer.add_figure("Ground truth", fig1, epoch)
                    self.tb_writer.add_figure("Prediction", fig2, epoch)

            self.save_checkpoint()
            self.epoch += 1
            
        self.tb_writer.close()
        print('Training finished!')

if __name__ == '__main__':
    args = parse.parse_args()
    os.makedirs(f'./ckpts/ns_V1e-5_T20_{args.phyloss_weight}phyloss_{args.train_obj}', exist_ok=True)
    save_config(args, f'./ckpts/ns_V1e-5_T20_{args.phyloss_weight}phyloss_{args.train_obj}')

    model = Unet3D(
        channels=1,
        cond_channels=1,
        channel_mults=(1, 2, 4, 8, 16),
        init_conv_channels=32,
        init_conv_kernel_size=5
    )
    diffusion_model = GaussianDiffusion(
        model=model,
        sample_size=(1, 20, 64, 64),
        timesteps=1000,
        objective=args.train_obj,
        physics_loss_weight=args.phyloss_weight
    )
    diffusion_model = diffusion_model.to(args.device_no)
    trainer = Trainer(diffusion_model=diffusion_model, **vars(args))
    set_random_seed(seed=1234, benchmark=False)
    trainer.train()


