import torch
from torch import optim
from torch.autograd import Variable
from torch import nn
import torch.nn.functional as F
import numpy as np
import pickle
from utils import myDataset
from model import Encoder
from model import Decoder
from model import SpeakerClassifier
from model import WeakSpeakerClassifier
#from model import LatentDiscriminator
from model import PatchDiscriminator
from model import CBHG
import os
from utils import Hps
from utils import Logger
from utils import DataLoader
from utils import to_var
from utils import reset_grad
from utils import multiply_grad
from utils import grad_clip
from utils import cal_acc
from utils import cc
from utils import calculate_gradients_penalty
from utils import gen_noise
from preprocess.tacotron.norm_utils import spectrogram2wav, get_spectrograms
#from preprocess.tacotron import utils

class Solver(object):
    def __init__(self, hps, data_loader, log_dir='./log/'):
        self.hps = hps
        self.data_loader = data_loader
        self.model_kept = []
        self.max_keep = 100
        self.build_model()
        self.logger = Logger(log_dir)

    def build_model(self):
        hps = self.hps
        ns = self.hps.ns
        emb_size = self.hps.emb_size
        self.Encoder = cc(Encoder(ns=ns, dp=hps.enc_dp))
        self.Decoder = cc(Decoder(ns=ns, c_a=hps.n_speakers, emb_size=emb_size))
        self.Generator = cc(Decoder(ns=ns, c_a=hps.n_speakers, emb_size=emb_size))
        self.SpeakerClassifier = cc(SpeakerClassifier(ns=ns, n_class=hps.n_speakers, dp=hps.dis_dp))
        self.GoodClassifier = cc(SpeakerClassifier(ns=ns, n_class=hps.n_speakers, dp=hps.dis_dp))
        self.PatchDiscriminator = cc(nn.DataParallel(PatchDiscriminator(ns=ns, n_class=hps.n_speakers)))
        betas = (0.5, 0.9)
        params = list(self.Encoder.parameters()) + list(self.Decoder.parameters())
        self.ae_opt = optim.Adam(params, lr=self.hps.lr, betas=betas)
        self.clf_opt = optim.Adam(self.SpeakerClassifier.parameters(), lr=self.hps.lr, betas=betas)
        self.good_clf_opt = optim.Adam(self.SpeakerClassifier.parameters(), lr=self.hps.lr, betas=betas)
        self.gen_opt = optim.Adam(self.Generator.parameters(), lr=self.hps.lr, betas=betas)
        self.patch_opt = optim.Adam(self.PatchDiscriminator.parameters(), lr=self.hps.lr, betas=betas)

    def save_good_classifier(self):
        output_path = os.path.join(os.path.abspath(os.getcwd()), 'good_classifier.pkl')

        classifier = {
            'good_classifier': self.GoodClassifier.state_dict()
        }

        with open(output_path, 'wb') as f_out:
            torch.save(classifier, f_out)


    def save_model(self, model_path, iteration, enc_only=True):
        if not enc_only:
            all_model = {
                'encoder': self.Encoder.state_dict(),
                'decoder': self.Decoder.state_dict(),
                'generator': self.Generator.state_dict(),
                'classifier': self.SpeakerClassifier.state_dict(),
                'patch_discriminator': self.PatchDiscriminator.state_dict(),
            }
        else:
            all_model = {
                'encoder': self.Encoder.state_dict(),
                'decoder': self.Decoder.state_dict(),
                'generator': self.Generator.state_dict(),
            }
        new_model_path = '{}-{}'.format(model_path, iteration)
        with open(new_model_path, 'wb') as f_out:
            torch.save(all_model, f_out)
        self.model_kept.append(new_model_path)

        if len(self.model_kept) >= self.max_keep:
            os.remove(self.model_kept[0])
            self.model_kept.pop(0)

    def load_model(self, model_path, enc_only=True, good_clf=False):
        print('load model from {}'.format(model_path))
        with open(model_path, 'rb') as f_in:
            all_model = torch.load(f_in)
            self.Encoder.load_state_dict(all_model['encoder'])
            self.Decoder.load_state_dict(all_model['decoder'])
            self.Generator.load_state_dict(all_model['generator'])
            if not enc_only:
                self.SpeakerClassifier.load_state_dict(all_model['classifier'])
                self.PatchDiscriminator.load_state_dict(all_model['patch_discriminator'])

    def load_good_classifier(self):
        print('Intentando cargar')
        with open('/home/julian/Documentos/multitarget-voice-conversion-vctk_CHOU_CONVERT/good_classifier.pkl', 'rb') as f_in:
            model = torch.load(f_in)
            print(model)
            self.GoodClassifier.load_state_dict(model['good_classifier'])

    def set_eval(self):
        self.Encoder.eval()
        self.Decoder.eval()
        self.Generator.eval()
        self.SpeakerClassifier.eval()
        self.GoodClassifier.eval()
        self.PatchDiscriminator.eval()

    def test_step(self, x, c, gen=False):
        self.set_eval()
        print("="*20 + " Test " + "="*20)

        # Check X content and dimensions
        print("="*20 + " X " + "="*20)
        x = to_var(x).permute(0, 2, 1)
        # print(x)
        print(x.size())

        # Check C content and dimensions
        print("="*20 + " C " + "="*20)
        # print(c)
        print(c.size())

        # Check the encoded content and dimensionsk
        print("="*20 + " Encoded " + "="*20)
        enc = self.Encoder(x)
        # print(enc)
        print(enc.size())


        x_tilde = self.Decoder(enc, c)

        # classify speaker
        # Check logits content and dimensions
        logits = self.clf_step(enc)
        # print(logits)
        print("="*20 + " Logits " + "="*20)
        print(logits.size())
        
        print("="*20 + " Loss clf " + "="*20)
        loss_clf = self.cal_loss(logits, c)
        print(loss_clf)

        if gen:
            x_tilde += self.Generator(enc, c)
        return x_tilde.data.cpu().numpy()

    def permute_data(self, data):
        C = to_var(data[0], requires_grad=False)
        X = to_var(data[1]).permute(0, 2, 1)
        return C, X

    def sample_c(self, size):
        n_speakers = self.hps.n_speakers
        c_sample = Variable(
                torch.multinomial(torch.ones(n_speakers), num_samples=size, replacement=True),  
                requires_grad=False)
        c_sample = c_sample.cuda() if torch.cuda.is_available() else c_sample
        return c_sample

    def encode_step(self, x):
        enc = self.Encoder(x)
        return enc

    def decode_step(self, enc, c):
        x_tilde = self.Decoder(enc, c)
        return x_tilde

    def patch_step(self, x, x_tilde, is_dis=True):
        D_real, real_logits = self.PatchDiscriminator(x, classify=True)
        D_fake, fake_logits = self.PatchDiscriminator(x_tilde, classify=True)
        if is_dis:
            w_dis = torch.mean(D_real - D_fake)
            gp = calculate_gradients_penalty(self.PatchDiscriminator, x, x_tilde)
            return w_dis, real_logits, gp
        else:
            return -torch.mean(D_fake), fake_logits

    def gen_step(self, enc, c):
        x_gen = self.Decoder(enc, c) + self.Generator(enc, c)
        return x_gen 

    def clf_step(self, enc):
        logits = self.SpeakerClassifier(enc)
        return logits

    def good_clf_step(self, enc):
        logits = self.GoodClassifier(enc)
        return logits

    def cal_loss(self, logits, y_true):
        print("logits")
        print(logits)
        # calculate loss 
        criterion = nn.CrossEntropyLoss()
        loss = criterion(logits, y_true)
        return loss

    def test_good_clf(self, x, c, gen=False):
        print('Voy a hacer el test_good_clf')
        self.set_eval()
        x = to_var(x).permute(0, 2, 1)
        enc = self.Encoder(x)

        logits = self.good_clf_step(enc)
        
        return logits

    def probar_clf_bueno(self, source):
        print('Probando el clasificador bueno')
        self.load_good_classifier()
        self.load_model('/home/julian/Documentos/PI_JCL/Experimentos/Experimento 5/single_sample_model_3_spanish_speakers.pkl-149999')


        print(type(self.GoodClassifier.dp))


        print('Ya cargué los modelos')
        _, spec = get_spectrograms(source)
        spec_expand = np.expand_dims(spec, axis=0)
        spec_tensor = torch.from_numpy(spec_expand).type(torch.FloatTensor)
        c = Variable(torch.from_numpy(np.array([int(3)]))).cuda()
        print('Preprocesé el audio de entrada')
        print(c)


        logits = self.test_good_clf(spec_tensor, c) # <-------------- el problema debe estar acá
        # print(logits)
        # # logits = logits.data.cpu().numpy()
        # # print(logits)
        # acc = cal_acc(logits, c)
        # print('Ya me dio')

        # print(acc)




        # data = next(self.data_loader)
        # c, x = self.permute_data(data)
        # g_clf = self.GoodClassifier(x)










    def train(self, model_path, flag='train', mode='train'):
        # load hyperparams
        hps = self.hps
        if mode == 'pretrain_G':
            for iteration in range(hps.enc_pretrain_iters):
                data = next(self.data_loader)
                c, x = self.permute_data(data)
                # encode
                enc = self.encode_step(x)
                x_tilde = self.decode_step(enc, c)
                loss_rec = torch.mean(torch.abs(x_tilde - x))
                reset_grad([self.Encoder, self.Decoder])
                loss_rec.backward()
                grad_clip([self.Encoder, self.Decoder], self.hps.max_grad_norm)
                self.ae_opt.step()
                # tb info
                info = {
                    f'{flag}/pre_loss_rec': loss_rec.item(),
                }
                slot_value = (iteration + 1, hps.enc_pretrain_iters) + tuple([value for value in info.values()])
                log = 'pre_G:[%06d/%06d], loss_rec=%.3f'
                print(log % slot_value)
                if iteration % 100 == 0:
                    for tag, value in info.items():
                        self.logger.scalar_summary(tag, value, iteration + 1)
        elif mode == 'train_good_classifier':
            # Load the pretrained model
            self.load_model('/home/julian/Documentos/PI_JCL/Experimentos/Experimento 5/single_sample_model_3_spanish_speakers.pkl-149999')
            
            for iteration in range(hps.dis_pretrain_iters):
                data = next(self.data_loader)
                c, x = self.permute_data(data)
                # encode
                enc = self.encode_step(x)
                # classify speaker
                logits = self.good_clf_step(enc)
                loss_clf = self.cal_loss(logits, c)
                # update 
                reset_grad([self.GoodClassifier])
                loss_clf.backward()
                grad_clip([self.GoodClassifier], self.hps.max_grad_norm)
                self.good_clf_opt.step()
                # calculate acc
                acc = cal_acc(logits, c)
                info = {
                    f'{flag}/pre_loss_clf': loss_clf.item(),
                    f'{flag}/pre_acc': acc,
                }
                slot_value = (iteration + 1, hps.dis_pretrain_iters) + tuple([value for value in info.values()])
                log = 'pre_D:[%06d/%06d], loss_clf=%.2f, acc=%.2f'
                print(log % slot_value)
                if iteration % 100 == 0:
                    for tag, value in info.items():
                        self.logger.scalar_summary(tag, value, iteration + 1)

            self.save_good_classifier()
        elif mode == 'pretrain_D':
            for iteration in range(hps.dis_pretrain_iters):
                data = next(self.data_loader)
                c, x = self.permute_data(data)
                print("="*50)
                print("c")
                print(c)
                print(c.size())
                # encode
                enc = self.encode_step(x)
                # classify speaker
                logits = self.clf_step(enc)
                print("="*50)
                print("logits")
                print(logits)
                print(logits.size())
                loss_clf = self.cal_loss(logits, c)
                # update 
                reset_grad([self.SpeakerClassifier])
                loss_clf.backward()
                grad_clip([self.SpeakerClassifier], self.hps.max_grad_norm)
                self.clf_opt.step()
                # calculate acc
                acc = cal_acc(logits, c)
                info = {
                    f'{flag}/pre_loss_clf': loss_clf.item(),
                    f'{flag}/pre_acc': acc,
                }
                slot_value = (iteration + 1, hps.dis_pretrain_iters) + tuple([value for value in info.values()])
                log = 'pre_D:[%06d/%06d], loss_clf=%.2f, acc=%.2f'
                print(log % slot_value)
                if iteration % 100 == 0:
                    for tag, value in info.items():
                        self.logger.scalar_summary(tag, value, iteration + 1)
        elif mode == 'patchGAN':
            for iteration in range(hps.patch_iters):
                #=======train D=========#
                for step in range(hps.n_patch_steps):
                    data = next(self.data_loader)
                    c, x = self.permute_data(data)
                    ## encode
                    enc = self.encode_step(x)
                    # sample c
                    c_prime = self.sample_c(x.size(0))
                    # generator
                    x_tilde = self.gen_step(enc, c_prime)
                    # discriminstor
                    w_dis, real_logits, gp = self.patch_step(x, x_tilde, is_dis=True)
                    # aux classification loss 
                    loss_clf = self.cal_loss(real_logits, c)
                    loss = -hps.beta_dis * w_dis + hps.beta_clf * loss_clf + hps.lambda_ * gp
                    reset_grad([self.PatchDiscriminator])
                    loss.backward()
                    grad_clip([self.PatchDiscriminator], self.hps.max_grad_norm)
                    self.patch_opt.step()
                    # calculate acc
                    acc = cal_acc(real_logits, c)
                    info = {
                        f'{flag}/w_dis': w_dis.item(),
                        f'{flag}/gp': gp.item(), 
                        f'{flag}/real_loss_clf': loss_clf.item(),
                        f'{flag}/real_acc': acc, 
                    }
                    slot_value = (step, iteration+1, hps.patch_iters) + tuple([value for value in info.values()])
                    log = 'patch_D-%d:[%06d/%06d], w_dis=%.2f, gp=%.2f, loss_clf=%.2f, acc=%.2f'
                    print(log % slot_value)
                    if iteration % 100 == 0:
                        for tag, value in info.items():
                            self.logger.scalar_summary(tag, value, iteration + 1)
                #=======train G=========#
                data = next(self.data_loader)
                c, x = self.permute_data(data)
                # encode
                enc = self.encode_step(x)
                # sample c
                c_prime = self.sample_c(x.size(0))
                # generator
                x_tilde = self.gen_step(enc, c_prime)
                # discriminstor
                loss_adv, fake_logits = self.patch_step(x, x_tilde, is_dis=False)
                # aux classification loss 
                loss_clf = self.cal_loss(fake_logits, c_prime)
                loss = hps.beta_clf * loss_clf + hps.beta_gen * loss_adv
                reset_grad([self.Generator])
                loss.backward()
                grad_clip([self.Generator], self.hps.max_grad_norm)
                self.gen_opt.step()
                # calculate acc
                acc = cal_acc(fake_logits, c_prime)
                info = {
                    f'{flag}/loss_adv': loss_adv.item(),
                    f'{flag}/fake_loss_clf': loss_clf.item(),
                    f'{flag}/fake_acc': acc, 
                }
                slot_value = (iteration+1, hps.patch_iters) + tuple([value for value in info.values()])
                log = 'patch_G:[%06d/%06d], loss_adv=%.2f, loss_clf=%.2f, acc=%.2f'
                print(log % slot_value)
                if iteration % 100 == 0:
                    for tag, value in info.items():
                        self.logger.scalar_summary(tag, value, iteration + 1)
                if iteration % 1000 == 0 or iteration + 1 == hps.patch_iters:
                    self.save_model(model_path, iteration + hps.iters)
        elif mode == 'train':
            for iteration in range(hps.iters):
                # calculate current alpha
                if iteration < hps.lat_sched_iters:
                    current_alpha = hps.alpha_enc * (iteration / hps.lat_sched_iters)
                else:
                    current_alpha = hps.alpha_enc
                #==================train D==================#
                for step in range(hps.n_latent_steps):
                    data = next(self.data_loader)
                    c, x = self.permute_data(data)
                    # encode
                    enc = self.encode_step(x)
                    # classify speaker
                    logits = self.clf_step(enc)
                    loss_clf = self.cal_loss(logits, c)
                    loss = hps.alpha_dis * loss_clf
                    # update 
                    reset_grad([self.SpeakerClassifier])
                    loss.backward()
                    grad_clip([self.SpeakerClassifier], self.hps.max_grad_norm)
                    self.clf_opt.step()
                    # calculate acc
                    acc = cal_acc(logits, c)
                    info = {
                        f'{flag}/D_loss_clf': loss_clf.item(),
                        f'{flag}/D_acc': acc,
                    }
                    slot_value = (step, iteration + 1, hps.iters) + tuple([value for value in info.values()])
                    log = 'D-%d:[%06d/%06d], loss_clf=%.2f, acc=%.2f'
                    print(log % slot_value)
                    if iteration % 100 == 0:
                        for tag, value in info.items():
                            self.logger.scalar_summary(tag, value, iteration + 1)
                #==================train G==================#
                data = next(self.data_loader)
                c, x = self.permute_data(data)
                # encode
                enc = self.encode_step(x)
                # decode
                x_tilde = self.decode_step(enc, c)
                loss_rec = torch.mean(torch.abs(x_tilde - x))
                # classify speaker
                logits = self.clf_step(enc)
                acc = cal_acc(logits, c)
                loss_clf = self.cal_loss(logits, c)
                # maximize classification loss
                loss = loss_rec - current_alpha * loss_clf
                reset_grad([self.Encoder, self.Decoder])
                loss.backward()
                grad_clip([self.Encoder, self.Decoder], self.hps.max_grad_norm)
                self.ae_opt.step()
                info = {
                    f'{flag}/loss_rec': loss_rec.item(),
                    f'{flag}/G_loss_clf': loss_clf.item(),
                    f'{flag}/alpha': current_alpha,
                    f'{flag}/G_acc': acc,
                }
                slot_value = (iteration + 1, hps.iters) + tuple([value for value in info.values()])
                log = 'G:[%06d/%06d], loss_rec=%.3f, loss_clf=%.2f, alpha=%.2e, acc=%.2f'
                print(log % slot_value)
                if iteration % 100 == 0:
                    for tag, value in info.items():
                        self.logger.scalar_summary(tag, value, iteration + 1)
                if iteration % 1000 == 0 or iteration + 1 == hps.iters:
                    self.save_model(model_path, iteration)

if __name__ == '__main__':
    hps = Hps()
    hps.load('./hps/v7.json')
    hps_tuple = hps.get_tuple()
    dataset = myDataset('/home/daniel/Documents/voice_integrador/vctk.h5',\
            '/home/daniel/Documents/programacion/multitarget-voice-conversion-vctk/preprocess/speaker_id_by_gender.json')
    data_loader = DataLoader(dataset)
    solver = Solver(hps_tuple, data_loader)