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

from models.dataset import *
from diffuser import GaussianDiffusion
from unet3d import Unet3D, EMA
from unet2d import Unet2D
from unet2d_spatial import Unet2D_Spatial
from physics_loss import *

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use('Agg')

parse = argparse.ArgumentParser()

parse.add_argument('--experiment', type=str, choices=['ns', 'burgers', 'darcy'], default='ns', help='experiment to run, default ns')
parse.add_argument('--device-no', type=int, default=0, help='device to use (default 0)')
parse.add_argument('--ema-decay', type=float, default=0.995, help='ema decay rate, default 0.995')
parse.add_argument('--epoch-start-ema', type=int, default=2, help='epoch to start ema, default 2')
parse.add_argument('--train-batch-size', type=int, default=4, help='batch size for training, default 4')
parse.add_argument('--test-batch-size', type=int, default=1, help='batch size for testing, default 1')
parse.add_argument('--train-obj', type=str, choices=['pred_noise', 'pred_x0'], default='pred_x0', help='object for nn, default pred_x0')
parse.add_argument('--train-lr', type=float, default=1e-4, help='learning rate for training, default 1e-4')
parse.add_argument('--train-lrf', type=float, default=0.1, help='learning rate factor for training, default 0.1')
parse.add_argument('--train-epochs', type=int, default=100, help='number of epochs for training, default 100')
parse.add_argument('--gamma', type=float, default=0.0, help='weight of physical loss, default 0.0')

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

def get_physics_informed_loss(loss_type: str, *args, **kwargs):
    if loss_type == 'ns':
        return NS_vorticity_loss(*args, **kwargs)
    elif loss_type == 'burgers':
        return Burgers_loss(*args, **kwargs)
    elif loss_type == 'darcy':
        return Darcy_loss(*args, **kwargs)
    else:
        raise ValueError('loss type not supported')

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
            gamma: float=0.1,
            train_dataset: torch.utils.data.Dataset=None,
            test_dataset: torch.utils.data.Dataset=None,
            tb_writer: SummaryWriter=None,
            experiment: str='ns',
            save_name: str=None,
            ):
        
        self.device = torch.device('cuda', device_no)
        self.diffusion_model = diffusion_model.to(self.device)
        self.ema = EMA(ema_decay)
        self.ema_model = copy.deepcopy(self.diffusion_model)
        self.epoch_start_ema = epoch_start_ema
        self.gamma = gamma
        self.train_obj = train_obj

        self.tb_writer = tb_writer
        self.experiment = experiment
        self.save_name = save_name
        self.train_ds = train_dataset
        self.test_ds = test_dataset

        self.train_dl = DataLoader(self.train_ds, batch_size=train_batch_size, shuffle=True, num_workers=8)
        self.test_dl = DataLoader(self.test_ds, batch_size=test_batch_size, shuffle=True, num_workers=8)

        self.optimizer = Adam(self.diffusion_model.model.parameters(), lr=train_lr)
        lf = lambda x: ((1 + math.cos(x*math.pi/train_epochs)) / 2) * (1 - train_lrf) + train_lrf
        self.scheduler = lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lf)

        self.train_epochs = train_epochs
        self.epoch = 0

        print("Trainer initialized, the network is objective to:", diffusion_model.objective)
    
    def save_checkpoint(self):
        torch.save(self.diffusion_model.model.state_dict(), '../ckpts/ddpm/'+self.save_name+'/ckpt.pt')
        print("Checkpoint saved")
    
    def reset_parameters(self):
        self.ema_model.model.load_state_dict(self.diffusion_model.model.state_dict())
    
    def step_ema(self):
        if self.epoch < self.epoch_start_ema:
            self.reset_parameters()
            return
        self.ema.update_model_average(self.ema_model.model, self.diffusion_model.model)

    def train(self):
        for epoch in range(self.epoch, self.train_epochs+1):

            self.diffusion_model.model.train()
            pbar_train = tqdm(self.train_dl, dynamic_ncols=True)
            cum_train_loss = 0
            for i, data in enumerate(pbar_train):
                x = data['x'].to(self.device)
                if self.experiment == 'ns':
                    x_prev = data['x_prev'].to(self.device)
                    x_next = data['x_next'].to(self.device)
                y = data['y'].to(self.device)

                x_pred, dn_loss = self.diffusion_model(x, cond=y)
                if self.experiment == 'burgers':
                    phy_loss = get_physics_informed_loss(self.experiment, u=x_pred, u0=x)
                elif self.experiment == 'ns':
                    phy_loss = get_physics_informed_loss(self.experiment, w=x_pred, w_prev=x_prev, w_next=x_next)
                elif self.experiment == 'darcy':
                    phy_loss = get_physics_informed_loss(self.experiment, a=x_pred, u=y, a0=x)
                loss = dn_loss + self.gamma * phy_loss
                cum_train_loss += loss.item()
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                pbar_train.set_description(f'Train Epoch {epoch}/{self.train_epochs}, Avg Loss: {cum_train_loss / (i+1):.4e}')
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
                    if self.experiment == 'ns':
                        x_prev = data['x_prev'].to(self.device)
                        x_next = data['x_next'].to(self.device)
                    y = data['y'].to(self.device)
                    x_pred, dn_loss = self.diffusion_model(x, cond=y)

                    if self.experiment == 'burgers':
                        phy_loss = get_physics_informed_loss(self.experiment, u=x_pred, u0=x)
                    elif self.experiment == 'ns':
                        phy_loss = get_physics_informed_loss(self.experiment, w=x_pred, w_prev=x_prev, w_next=x_next)
                    elif self.experiment == 'darcy':
                        phy_loss = get_physics_informed_loss(self.experiment, a=x_pred, u=y, a0=x)
                    loss = dn_loss + self.gamma * phy_loss
                    cum_val_loss += loss.item()
                    pbar_test.set_description(f'Val Epoch {epoch}/{self.train_epochs}, Loss: {cum_val_loss / (i+1):.4e}')
                    self.tb_writer.add_scalar('test/loss', loss.item(), epoch*len(self.test_dl)+i)
                    self.tb_writer.add_scalar('test/dn_loss', dn_loss.item(), epoch*len(self.test_dl)+i)
                    self.tb_writer.add_scalar('test/phy_loss', phy_loss.item(), epoch*len(self.test_dl)+i)
                
                if epoch % 20 == 0:
                    print(f"Sampling at epoch {epoch}")
                    x_pred = self.diffusion_model.sample(cond=y[:4])
                    x_pred = x_pred.detach().cpu().numpy().squeeze()
                    x = x.detach().cpu().numpy().squeeze()
                    # fig1, ax1 = plt.subplots(2, 4, figsize=(14, 6))
                    # ax1 = ax1.flatten()
                    # for i in range(8):
                    #     im1 = ax1[i].imshow(x[i])
                    #     ax1[i].axis('off')
                    # fig1.colorbar(im1, ax=ax1)
                    # fig2, ax2 = plt.subplots(2, 4, figsize=(14, 6))
                    # ax2 = ax2.flatten()
                    # for i in range(8):
                    #     im2 = ax2[i].imshow(x_pred[i])
                    #     ax2[i].axis('off')
                    # fig2.colorbar(im2, ax=ax2)
                    fig1, ax1 = plt.subplots(2, 2, figsize=(8, 8))
                    ax1 = ax1.flatten()
                    for i in range(4):
                        im1 = ax1[i].imshow(x[i], cmap='jet')
                        ax1[i].axis('off')
                    fig1.colorbar(im1, ax=ax1)
                    fig2, ax2 = plt.subplots(2, 2, figsize=(8, 8))
                    ax2 = ax2.flatten()
                    for i in range(4):
                        im2 = ax2[i].imshow(x_pred[i], cmap='jet')
                        ax2[i].axis('off')
                    fig2.colorbar(im2, ax=ax2) 
                    self.tb_writer.add_figure("Ground truth", fig1, epoch)
                    self.tb_writer.add_figure("Prediction", fig2, epoch)

            self.save_checkpoint()
            self.epoch += 1
            
        self.tb_writer.close()
        print('Training finished!')

if __name__ == '__main__':
    args = parse.parse_args()

    SAVE_NAME = args.experiment + '_' + str(args.gamma) + 'phyloss(ec)'
    os.makedirs('../ckpts/ddpm/' + SAVE_NAME, exist_ok=True)
    save_config(args, '../ckpts/ddpm/' + SAVE_NAME)


    model = Unet2D(
        channels=1,
        cond_channels=1,
        channel_mults=(1, 2, 4, 8),
        init_conv_channels=32,
        init_conv_kernel_size=5
    )

    diffusion_model = GaussianDiffusion(
        model=model,
        sample_size=(1, 100, 128),
        timesteps=800,
        objective=args.train_obj,
        output_mask=None
    )

    # model = Unet3D(
    #     channels=1,
    #     cond_channels=1,
    #     channel_mults=(1, 2, 4, 8, 16),
    #     init_conv_channels=32,
    #     init_conv_kernel_size=5
    # )
    # diffusion_model = GaussianDiffusion(
    #     model=model,
    #     sample_size=(1, 20, 64, 64),
    #     timesteps=1000,
    #     objective=args.train_obj,
    #     physics_loss_weight=args.phyloss_weight
    # )

    # model = Unet2D_Spatial(
    #     channels=1,
    #     cond_channels=1,
    #     channel_mults=(1, 2, 4, 8),
    #     init_conv_channels=32,
    #     init_conv_kernel_size=5
    # )
    # diffusion_model = GaussianDiffusion(
    #     model=model,
    #     sample_size=(1, 64, 64),
    #     timesteps=600,
    #     objective=args.train_obj,
    #     output_mask=Darcy_mask
    # )


    tb_writer = SummaryWriter(log_dir=f'./logs/' + SAVE_NAME)
    # train_dataset = NaiverStokes_Dataset("../data/ns_data_T20_v1e-03_N1800.mat")
    # test_dataset = NaiverStokes_Dataset("../data/ns_data_T20_v1e-03_N200.mat")
    train_dataset = Burgers_Dataset("../data/burgers_data_Nt100_v1e-02_N1800.mat")
    test_dataset = Burgers_Dataset("../data/burgers_data_Nt100_v1e-02_N200.mat")
    # train_dataset = Darcys_Dataset('../data/darcy_data_r64_N800.mat')
    # test_dataset = Darcys_Dataset('../data/darcy_data_r64_N200.mat')
    trainer = Trainer(diffusion_model=diffusion_model, 
                      train_dataset=train_dataset,
                      test_dataset=test_dataset,
                      tb_writer=tb_writer,
                      save_name=SAVE_NAME,
                      **vars(args))
    set_random_seed(seed=2345, benchmark=False)
    trainer.train()

