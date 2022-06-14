from random import random
import numpy as np
import pandas as pd
import torch
import torch.utils.data
import torch.optim as optim
from torch.optim import Adam
from torch.nn import functional as F
from torch.nn import (Dropout, LeakyReLU, Linear, Module, ReLU, Sequential,
Conv2d, ConvTranspose2d, BatchNorm2d, Sigmoid, init, BCELoss, CrossEntropyLoss,SmoothL1Loss)
from model.synthesizer.transformer import ImageTransformer,DataTransformer
from tqdm import tqdm
from model.synthesizer.ctabgan_synthesizer import CTABGANSynthesizer, Sampler, Generator, Discriminator, Classifier, apply_activate, determine_layers_disc, determine_layers_gen, get_st_ed, weights_init, cond_loss
from model.synthesizer.stacked_condvec_factory import StackedCondvecFactory

class CTABGANSecondLayer(CTABGANSynthesizer):

    """
    This class represents the main model used for training the model and generating synthetic data

    Variables:
    1) random_dim -> size of the noise vector fed to the generator
    2) class_dim -> tuple containing dimensionality of hidden layers for the classifier network
    3) num_channels -> no. of channels for deciding respective hidden layers of discriminator and generator networks
    4) dside -> height/width of the input data fed to discriminator network
    5) gside -> height/width of the input data generated by the generator network
    6) l2scale -> parameter to decide strength of regularization of the network based on constraining l2 norm of weights
    7) batch_size -> no. of records to be processed in each mini-batch of training
    8) epochs -> no. of epochs to train the model
    9) device -> type of device to be used for training (i.e., gpu/cpu)
    10) generator -> generator network from which data can be generated after training the model

    Methods:
    1) __init__() -> initializes the model with user specified parameters
    2) fit() -> takes the pre-processed training data and associated parameters as input to fit the CTABGANSynthesizer model 
    3) sample() -> takes as input the no. of data rows to be generated and synthesizes the corresponding no. of data rows

    """ 
    
    def __init__(self,
                 class_dim=(256, 256, 256, 256),
                 random_dim=100,
                 num_channels=64,
                 l2scale=1e-5,
                 batch_size=500,
                 epochs=1
                 ):
        super().__init__(
            class_dim = class_dim,
            random_dim = random_dim,
            num_channels = num_channels,
            l2scale=l2scale,
            batch_size=batch_size,
            epochs=epochs
        )
        
    def fit(self, train_data=pd.DataFrame, stacked_condvec_factory: StackedCondvecFactory = None, intermediate_data=None, categorical=[], mixed={}, type={}):
        
        # obtaining the column index of the target column used for ML tasks
        problem_type = None
        target_index = None
        
        if type:
            problem_type = list(type.keys())[0]
            if problem_type:
                target_index = train_data.columns.get_loc(type[problem_type])

        # transforming pre-processed training data according to different data types 
        # i.e., mode specific normalisation for numeric and mixed columns and one-hot-encoding for categorical columns
        self.transformer = DataTransformer(train_data=train_data, categorical_list=categorical, mixed_dict=mixed)
        self.transformer.fit() 
        train_data = self.transformer.transform(train_data.values)
        # storing column size of the transformed training data
        data_dim = self.transformer.output_dim
        
        # initializing the sampler object to execute training-by-sampling 
        data_sampler = Sampler(train_data, self.transformer.output_info)

        # obtaining the desired height/width for converting tabular data records to square images for feeding it to discriminator network 		
        sides = [4, 8, 16, 24, 32]
        # the discriminator takes the transformed training data concatenated by the corresponding conditional vectors as input
        col_size_d = data_dim + stacked_condvec_factory.n_opt
        for i in sides:
            if i * i >= col_size_d:
                self.dside = i
                break
        
        # obtaining the desired height/width for generating square images from the generator network that can be converted back to tabular domain 		
        sides = [4, 8, 16, 24, 32]
        col_size_g = data_dim
        for i in sides:
            if i * i >= col_size_g:
                self.gside = i
                break
		
  
        # get width of intermediate table
        input_height, input_width =  np.shape(intermediate_data)
  
        # constructing the generator and discriminator networks
        layers_G = determine_layers_gen(self.gside, input_width+stacked_condvec_factory.n_opt, self.num_channels)
        layers_D = determine_layers_disc(self.dside, self.num_channels)
        self.generator = Generator(layers_G).to(self.device)
        discriminator = Discriminator(layers_D).to(self.device)
        
        # assigning the respective optimizers for the generator and discriminator networks
        optimizer_params = dict(lr=2e-4, betas=(0.5, 0.9), eps=1e-3, weight_decay=self.l2scale)
        optimizerG = Adam(self.generator.parameters(), **optimizer_params)
        optimizerD = Adam(discriminator.parameters(), **optimizer_params)

       
        st_ed = None
        classifier=None
        optimizerC= None
        if target_index != None:
            # obtaining the one-hot-encoding starting and ending positions of the target column in the transformed data
            st_ed= get_st_ed(target_index,self.transformer.output_info)
            # configuring the classifier network and it's optimizer accordingly 
            classifier = Classifier(data_dim,self.class_dim,st_ed).to(self.device)
            optimizerC = optim.Adam(classifier.parameters(),**optimizer_params)
        
        # initializing learnable parameters of the discrimnator and generator networks  
        self.generator.apply(weights_init)
        discriminator.apply(weights_init)

        # initializing the image transformer objects for the generator and discriminator networks for transitioning between image and tabular domain 
        self.Gtransformer = ImageTransformer(self.gside)       
        self.Dtransformer = ImageTransformer(self.dside)
        
        # initiating the training by computing the number of iterations per epoch
        steps_per_epoch = max(1, len(train_data) // self.batch_size)
        for i in tqdm(range(self.epochs)):
            for j in range(steps_per_epoch):
                
                # sampling rows from previous run
                lb_itmd_data = j * self.batch_size
                ub_itmd_data = (j+1) * self.batch_size
                
                if self.batch_size * (j+1) > len(train_data):
                    self.batch_size = len(train_data)
                
                itmd_vector_batch = intermediate_data[lb_itmd_data:ub_itmd_data]

                # sampling conditional vectors from previous run
                condvec = stacked_condvec_factory.sample_next_layers(j)
                c, m, col, opt = condvec
                c = torch.from_numpy(c).to(self.device)
                m = torch.from_numpy(m).to(self.device)
                itmd_vector_batch = torch.from_numpy(itmd_vector_batch).to(self.device)
                
                # concatenating conditional vectors and converting resulting intermediate
                # vectors into the image domain to be fed to the generator as input
                itmd_noise = torch.cat([itmd_vector_batch, c], dim=1)
                itmd_noise = itmd_noise.view(self.batch_size,input_width+stacked_condvec_factory.n_opt,1,1)

                # sampling real data according to the conditional vectors and shuffling
                # it before feeding to discriminator to isolate conditional loss on generator    
                perm = np.arange(self.batch_size)
                np.random.shuffle(perm)
                real = data_sampler.sample(self.batch_size, col[perm], opt[perm])
                real = torch.from_numpy(real.astype('float32')).to(self.device)
                
                # storing shuffled ordering of the conditional vectors
                c_perm = c[perm]
                # generating synthetic data as an image
                fake = self.generator(itmd_noise)
                # converting it into the tabular domain as per format of the trasformed training data
                faket = self.Gtransformer.inverse_transform(fake)
                # applying final activation on the generated data (i.e., tanh for numeric and gumbel-softmax for categorical)
                fakeact = apply_activate(faket, self.transformer.output_info)

                # the generated data is then concatenated with the corresponding condition vectors 
                fake_cat = torch.cat([fakeact, c], dim=1)
                # the real data is also similarly concatenated with corresponding conditional vectors    
                real_cat = torch.cat([real, c_perm], dim=1)
                
                # transforming the real and synthetic data into the image domain for feeding it to the discriminator
                real_cat_d = self.Dtransformer.transform(real_cat)
                fake_cat_d = self.Dtransformer.transform(fake_cat)

                # executing the gradient update step for the discriminator    
                optimizerD.zero_grad()
                # computing the probability of the discriminator to correctly classify real samples hence y_real should ideally be close to 1
                y_real,_ = discriminator(real_cat_d)
                # computing the probability of the discriminator to correctly classify fake samples hence y_fake should ideally be close to 0
                y_fake,_ = discriminator(fake_cat_d)
                # computing the loss to essentially maximize the log likelihood of correctly classifiying real and fake samples as log(D(x))+log(1−D(G(z)))
                # or equivalently minimizing the negative of log(D(x))+log(1−D(G(z))) as done below
                loss_d = (-(torch.log(y_real + 1e-4).mean()) - (torch.log(1. - y_fake + 1e-4).mean()))
                # accumulating gradients based on the loss
                loss_d.backward()
                # computing the backward step to update weights of the discriminator
                optimizerD.step()

                # similarly sample noise vectors and conditional vectors
                noisez = torch.randn(self.batch_size, self.random_dim, device=self.device)
                
                
                
                # condvec = stacked_condvec_factory.sample_train(self.batch_size)
                # c, m, col, opt = condvec
                # c = torch.from_numpy(c).to(self.device)
                # m = torch.from_numpy(m).to(self.device)
                
                # noisez = torch.cat([noisez, c], dim=1)
                # noisez =  noisez.view(self.batch_size,self.random_dim+stacked_condvec_factory.n_opt,1,1)

                # executing the gradient update step for the generator    
                optimizerG.zero_grad()

                # similarly generating synthetic data and applying final activation
                fake = self.generator(itmd_noise)
                faket = self.Gtransformer.inverse_transform(fake)
                fakeact = apply_activate(faket, self.transformer.output_info)
                # concatenating conditional vectors and converting it to the image domain to be fed to the discriminator
                fake_cat = torch.cat([fakeact, c], dim=1) 
                fake_cat = self.Dtransformer.transform(fake_cat)

                # computing the probability of the discriminator classifiying fake samples as real 
                # along with feature representaions of fake data resulting from the penultimate layer 
                y_fake,info_fake = discriminator(fake_cat)
                # extracting feature representation of real data from the penultimate layer of the discriminator 
                _,info_real = discriminator(real_cat_d)
                # computing the conditional loss to ensure the generator generates data records with the chosen category as per the conditional vector
                cross_entropy = cond_loss(faket, self.transformer.output_info, c, m)
                
                # computing the loss to train the generator where we want y_fake to be close to 1 to fool the discriminator 
                # and cross_entropy to be close to 0 to ensure generator's output matches the conditional vector  
                g = -(torch.log(y_fake + 1e-4).mean()) + cross_entropy
                # in order to backprop the gradient of separate losses w.r.t to the learnable weight of the network independently
                # we may use retain_graph=True in backward() method in the first back-propagated loss 
                # to maintain the computation graph to execute the second backward pass efficiently
                g.backward(retain_graph=True)
                # computing the information loss by comparing means and stds of real/fake feature representations extracted from discriminator's penultimate layer
                loss_mean = torch.norm(torch.mean(info_fake.view(self.batch_size,-1), dim=0) - torch.mean(info_real.view(self.batch_size,-1), dim=0), 1)
                loss_std = torch.norm(torch.std(info_fake.view(self.batch_size,-1), dim=0) - torch.std(info_real.view(self.batch_size,-1), dim=0), 1)
                loss_info = loss_mean + loss_std 
                # computing the finally accumulated gradients
                loss_info.backward()
                # executing the backward step to update the weights
                optimizerG.step()

                # the classifier module is used in case there is a target column associated with ML tasks 
                if problem_type:
                    
                    c_loss = None
                    # in case of binary classification, the binary cross entropy loss is used 
                    if (st_ed[1] - st_ed[0])==2:
                        c_loss = BCELoss()
                    # in case of multi-class classification, the standard cross entropy loss is used
                    else: c_loss = CrossEntropyLoss() 
                    
                    # updating the weights of the classifier
                    optimizerC.zero_grad()
                    # computing classifier's target column predictions on the real data along with returning corresponding true labels
                    real_pre, real_label = classifier(real)
                    if (st_ed[1] - st_ed[0])==2:
                        real_label = real_label.type_as(real_pre)
                    # computing the loss to train the classifier so that it can perform well on the real data
                    loss_cc = c_loss(real_pre, real_label)
                    loss_cc.backward()
                    optimizerC.step()
                    
                    # updating the weights of the generator
                    optimizerG.zero_grad()
                    # generate synthetic data and apply the final activation
                    fake = self.generator(itmd_noise)
                    faket = self.Gtransformer.inverse_transform(fake)
                    fakeact = apply_activate(faket, self.transformer.output_info)
                    # computing classifier's target column predictions on the fake data along with returning corresponding true labels
                    fake_pre, fake_label = classifier(fakeact)
                    if (st_ed[1] - st_ed[0])==2:
                        fake_label = fake_label.type_as(fake_pre)
                    # computing the loss to train the generator to improve semantic integrity between target column and rest of the data
                    loss_cg = c_loss(fake_pre, fake_label)
                    loss_cg.backward()
                    optimizerG.step()

    
    def sample(self, intermediate_data, n, stacked_condvec_factory):
        # turning the generator into inference mode to effectively use running statistics in batch norm layers
        self.generator.eval()
        # column information associated with the transformer fit to the pre-processed training data
        output_info = self.transformer.output_info
        
        # get width of intermediate table
        input_height, input_width =  np.shape(intermediate_data)
        
        # generating synthetic data in batches accordingly to the total no. required
        steps = n // self.batch_size + 1
        data = []        
        
        print("Stacked condvec factory n_opt first layer: " + str(stacked_condvec_factory.n_opt))
        
        for i in range(steps):
            # generating synthetic data using previous output and conditional vectors
            lb_itmd_data = i * self.batch_size
            ub_itmd_data = (i+1) * self.batch_size
                
            if self.batch_size * (i+1) > len(intermediate_data):
                self.batch_size = len(intermediate_data)
            
            
            # sampling rows from previous run
            
            itmd_vector_batch = intermediate_data[lb_itmd_data:ub_itmd_data]
            itmd_vector_batch = torch.from_numpy(itmd_vector_batch).to(self.device)
            
            condvec = stacked_condvec_factory.sample_next_layers(i)
            c = condvec
            
            c = torch.from_numpy(c).to(self.device)
            itmd_noise = torch.cat([itmd_vector_batch, c], dim=1)
            itmd_noise = itmd_noise.view(self.batch_size,input_width+stacked_condvec_factory.n_opt,1,1)
            
            
            fake = self.generator(itmd_noise)
            faket = self.Gtransformer.inverse_transform(fake)
            fakeact = apply_activate(faket,output_info)

            data.append(fakeact.detach().cpu().numpy())

        data = np.concatenate(data, axis=0)
        result = self.transformer.inverse_transform(data)

        return result[0:n] 