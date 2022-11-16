import logging, os, datetime, time, sys
ROOT_PATH = os.path.abspath(os.path.dirname(__file__)).split('framework')[0] + 'framework'
sys.path.append(ROOT_PATH)

from dataloader import RatMixData, UserHisData, UserTestData, pad_collate_valid
from model import TowerModel, MFModel
from debias import Base_Debias, Pop_Debias, EstPop_Debias, ReSample_Debias, MixNeg_Debias, BatchMixup_Debias
import eval as eval
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from numpy.random import beta


def get_logger(filename, verbosity=1, name=None):
    filename = filename
    level_dict = {0: logging.DEBUG, 1: logging.INFO, 2: logging.WARNING}
    formatter = logging.Formatter(
        "[%(asctime)s][%(filename)s][line:%(lineno)d][%(levelname)s] %(message)s"
    )
    logger = logging.getLogger(name)
    logger.setLevel(level_dict[verbosity])

    fh = logging.FileHandler(filename, "w")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return logger


class Trainer:
    def __init__(self, config):
        self.config = config
        self.device = torch.device(config['device'])
        self.init_logger()
        self.set_seed()

    def init_logger(self):
        if not os.path.exists(self.config['log_path']):
            os.makedirs(self.config['log_path'])

        ISOTIMEFORMAT = '%m%d-%H%M%S'
        timestamp = str(datetime.datetime.now().strftime(ISOTIMEFORMAT))
        seed = 'seed' + str(self.config['seed'])

        sampled_flag = 'sampled_' + str(self.config['sample_size']) if self.config['sample_from_batch'] is True else 'full'
        log_name = '_'.join((self.config['data_name'], str(self.config['batch_size']), str(self.config['debias']), sampled_flag, str(self.config['learning_rate']), seed, timestamp))
        os.makedirs(os.path.join(self.config['log_path'], log_name))
        log_file_name = os.path.join(self.config['log_path'], log_name)
        self.writer = SummaryWriter(log_dir=log_file_name)
        
        logname = log_file_name + '/log.txt'
        self.logger = get_logger(logname)
        self.logger.info(self.config)

    def set_seed(self):
        if self.config['fix_seed']:
            import os
            seed = self.config['seed']
            os.environ['PYTHONHASHSEED']=str(seed)

            import random
            random.seed(seed)
            np.random.seed(seed)
            
            import torch
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True

    def load_dataset(self):
        mldata = RatMixData(self.config['data_dir'],self.config['data_name'])

        train_mat, test_mat = mldata.get_train_test()
        (M, N) = train_mat.shape
        self.logger.info('Number of Users/Items, {}/{}'.format(M, N))
        self.item_num = N + 1
        return train_mat, test_mat
    
    def model_init(self, train_mat):
        (user_num, item_num) = train_mat.shape
        if self.config['model'].lower() == 'mf':
            return MFModel(user_num, item_num, self.config['emb_dim']).to(self.device)
        else:
            raise ValueError('Not supported model types')
    
    def config_optimizers(self, parameters, lr, wd):
        if self.config['optim'].lower() == 'adam':
            return optim.Adam(parameters, lr=lr, weight_decay=wd) 
        elif self.config['optim'].lower() == 'sgd':
            return optim.SGD(parameters, lr=lr, weight_decay=wd)
        else:
            raise NotImplementedError

    def topk(self, model, query, k, user_h=None):
        more = user_h.size(1) if user_h is not None else 0
        # embedding的每一行可以看作是每一个item的representation
        # 因此可直接与query相乘得到对应query和item的分数
        # 从而得到每一个query对应的topK个item
        score, topk_items = torch.topk(model.scorer(query, model.item_encoder.weight[1:]), k + more)
        if user_h is not None:
            topk_items += 1
            existing, _ = user_h.sort()
            idx_ = torch.searchsorted(existing, topk_items)
            idx_[idx_ == existing.size(1)] = existing.size(1) - 1
            score[torch.gather(existing, 1, idx_) == topk_items] = -float('inf')
            score1, idx = score.topk(k)
            return score1, torch.gather(topk_items, 1, idx)
        else:
            return score, topk_items  # B X K, B x K

    def _test_step(self, model, test_data, eval_metric, cutoffs):
        user_id, user_his, user_cand, user_rating = test_data
        user_id, user_his, user_cand, user_rating = user_id.to(self.device), user_his.to(self.device), user_cand.to(self.device), user_rating.to(self.device)
        rank_m = eval.get_rank_metrics(eval_metric)  # 评价指标
        topk = self.config['topk']
        bs = user_id.size(0)
        query = model.construct_query(user_id)
        score, topk_items = self.topk(model, query, topk, user_his)
        if user_cand.dim() > 1:
            target, _ = user_cand.sort()
            idx_ = torch.searchsorted(target, topk_items)  # B x K
            # 当target中不存在topk_items中的元素时 得到的索引是target.size(1)
            idx_[idx_ == target.size(1)] = target.size(1) - 1
            label = torch.gather(target, 1, idx_) == topk_items
            pos_rating = user_rating
        else:
            label = user_cand.view(-1, 1) == topk_items
            pos_rating = user_rating.view(-1, 1)
        return [func(label, pos_rating, cutoff) for cutoff in cutoffs for name, func in rank_m], bs

    def evaluate(self, model, test_loader):
        model.eval()
        eval_metric = self.config['metrics']
        cutoffs = self.config['cutoffs']
        out_res = []
        for batch_idx, test_data in enumerate(test_loader):
            outputs = self._test_step(model, test_data, eval_metric, cutoffs)
            out_res.append(outputs)

        metric, bs = zip(*out_res)
        metric = torch.tensor(metric)
        bs = torch.tensor(bs)
        out = (metric * bs.view(-1, 1)).sum(0) / bs.sum()
        metrics = [f"{v}@{c}" for c in cutoffs for v in eval_metric]
        out = dict(zip(metrics, out))
        return out

    def _train_step(self, user_id, item_id, model:TowerModel, debias: Base_Debias, **kwargs):
        # Embedding
        query = model.construct_query(user_id)
        item_emb = model.item_encoder(item_id)
        # B = batch_size
        B = user_id.shape[0]

        # scores[i][j]表示第i个user和第j个item点乘的score
        scores = torch.matmul(query, item_emb.T)
        # 论文中提到的修正项logP
        log_pos_prob = debias(item_id)
        # 取对角线元素 即item[i]和user[i]的点乘分数作为正样本
        pos_rat = torch.diag(scores)

        # 从batch中采样B组负样本
        if self.config['sample_from_batch']:
            # Only resample method have sample_from_batch
            sample_size = self.config['sample_size']
            assert 1 < sample_size < B, ValueError('The number of samples must be greater than 1 and smaller than '
                                                   'batch_size')
            # Actually, 'replacement=True'
            # 随机生成索引矩阵 因为正样本有B个 因此采样B次每次采样sample_size个负样本
            IndM = torch.randint(B, size=(B, sample_size), device=self.device)
            # 取出每组负样本的修正项
            log_neg_prob = log_pos_prob[IndM]
            # 取出每组负样本的分数 由于是对矩阵操作因此用gather取元素
            neg_rat = torch.gather(scores, 1, IndM)
        else:
            log_neg_prob = log_pos_prob.view(1, -1).repeat(B, 1)
            neg_rat = scores

        loss = model.loss(pos_rat, log_pos_prob, neg_rat, log_neg_prob)
        return loss

    def _fit(self, model:TowerModel, debias:Base_Debias, train_loader:DataLoader, test_loader=None):
        num_epoch = self.config['epoch']
        optimizer = self.config_optimizers(model.parameters(), self.config['learning_rate'], self.config['weight_decay'])
        
        if self.config['steprl'] :
            scheduler = optim.lr_scheduler.StepLR(optimizer, self.config['step_size'], self.config['step_gamma'])

        for epoch in range(num_epoch):
            loss_ = 0.0
            
            for batch_idx, batch_data in enumerate(train_loader):
                # print('batch data: ', batch_data)
                # return
                model.train()
                debias.train()

                optimizer.zero_grad()

                user_id, item_id = batch_data  # (tensor Bx1, tensor Bx1)
                user_id, item_id = user_id.to(self.device), item_id.to(self.device)

                # 在这里进行mixup

                loss = self._train_step(user_id, item_id, model, debias, batch_idx=batch_idx)

                loss_ += loss.detach()
                loss.backward()
                optimizer.step()

            if self.config['steprl'] :
                scheduler.step()
            self.writer.add_scalar("Train/Loss", loss_/(batch_idx+1.0), epoch)
            self.logger.info('Epoch {}'.format(epoch))
            self.logger.info('***************Train loss {:.8f}'.format(loss_))

            if ((epoch % self.config['valid_interval']) == 0) or (epoch >= num_epoch - 1):
                with torch.no_grad():
                    out = self.evaluate(model, test_loader)

                for k in out.keys():
                    self.writer.add_scalar("Evaluate/{}".format(k), out[k], epoch)
                ress = (', ').join(["{} : {:.6f}".format(k, out[k]) for k in out.keys()])
                    
                self.logger.info('***************Eval_Res ' + ress)
        
            self.writer.flush()

    def fit(self, train_mat, test_mat):
        train_data = UserHisData(train_mat=train_mat)
        train_loader = DataLoader(train_data, batch_size=self.config['batch_size'], num_workers=self.config['num_workers'], shuffle=True, pin_memory=True)
        test_data = UserTestData(train_mat=train_mat, test_mat=test_mat)
        test_loader = DataLoader(test_data, batch_size=self.config['eval_batch_size'], collate_fn=pad_collate_valid, num_workers=self.config['num_workers'])
        model = self.model_init(train_mat=train_mat)

        #=========================================
        # Define bias mmodule
        # Base debias : uniform, Pop debias : pop
        if self.config['debias'] == 1 :
            """ base debias, uniform sampling  """
            debias_module = Base_Debias(train_mat.shape[1], self.device, mode=self.config['pop_mode'])
        elif self.config['debias'] in [2, 5]:
            """ debias with popularity   """
            pop_count = train_mat.sum(axis=0).A.squeeze()
            debias_module = Pop_Debias(pop_count, self.device, mode=self.config['pop_mode'])
        elif self.config['debias'] in [3, 6]:
            pop_count = train_mat.sum(axis=0).A.squeeze()
            debias_module = ReSample_Debias(pop_count, self.device, mode=self.config['pop_mode'])
        elif self.config['debias'] == 4:
            pop_count = train_mat.sum(axis=0).A.squeeze()
            debias_module = MixNeg_Debias(pop_count, self.device, mode=self.config['pop_mode'])
        elif self.config['debias'] in [7,9]:
            debias_module = EstPop_Debias(train_mat.shape[1], self.device, self.config['alpha'], mode=self.config['pop_mode'])
        elif self.config['debias'] == 10:
            pop_count = train_mat.sum(axis=0).A.squeeze()
            debias_module = BatchMixup_Debias(pop_count, self.device, mode=self.config['pop_mode'])
        else:
            raise NotImplementedError
        
        debias_module = debias_module.to(self.device)
        #=========================================

        # for idx, data in enumerate(test_loader):
            # user_id, user_his, user_cand, user_rating = data
            # print(user_id)
            # print(user_his, user_his.size())
            # print(user_cand, user_cand.size())
            # print(user_rating, user_rating.size())
            # return
        # for (x, y) in train_loader:
        #     print(x)
        #     print(y)
        #     return

        self._fit(model, debias_module, train_loader, test_loader)


class Trainer_Resample(Trainer):
    def __init__(self, config):
        super().__init__(config)
    
    def _train_step(self, user_id, item_id, model: TowerModel, debias: ReSample_Debias, **kwargs):
        query = model.construct_query(user_id)
        item_emb = model.item_encoder(item_id)

        # generate the index matrix of items
        B = user_id.shape[0]
        # B = self.config['batch_size']
        if self.config['sample_from_batch']:
            sample_size = min(B, self.config['sample_size'])
        else:
            sample_size = B
        log_pop_bias = debias.get_pop_bias(item_id)

        
        scores = torch.matmul(query, item_emb.T)
        log_pos_prob, IndM, log_neg_prob = debias.resample(scores, log_pop_bias, sample_size)

        pos_rat = torch.diag(scores)
        neg_rat = torch.gather(scores, 1, IndM)
        loss = model.loss(pos_rat, log_pos_prob, neg_rat, log_neg_prob)
        return loss


class Trainer_MixNeg(Trainer):
    """
        Mixed Negative Sampling for Learning Two-tower Neural Networks in Recommendations
    """
    def __init__(self, config):
        super().__init__(config)
    
    def _train_step(self, user_id, item_id, model: TowerModel, debias: MixNeg_Debias, **kwargs):
        query = model.construct_query(user_id)
        item_emb = model.item_encoder(item_id)

        # Uniformly sample items
        # B = user_id.shape[0]
        sample_size = self.config['sample_size']
        # 从全局词典均匀采样sample_size个负样本
        mixed_items = torch.randint(self.item_num, size=(sample_size,), device=self.device)
        mixed_item_emb = model.item_encoder(mixed_items)

        items = torch.cat([item_id, mixed_items], dim=-1)

        # 正样本修正项logQ
        log_pos_prob = debias.get_pop_bias(item_id)
        # 根据样本数确定混合分布比例
        ratio = (sample_size * 1.0) / (sample_size + self.config['batch_size'])

        log_neg_prob = debias(items, ratio=ratio)
        pop_scores = torch.matmul(query, item_emb.T)
        uni_scores = torch.matmul(query, mixed_item_emb.T)

        pos_rat = torch.diag(pop_scores)
        neg_rat = torch.cat([pop_scores, uni_scores], dim=-1)

        loss = model.loss(pos_rat, log_pos_prob, neg_rat, log_neg_prob)
        return loss


class Trainer_Mixup(Trainer):
    """
        batch mixup + batch negative sampling
        loss function is CE or BPR
    """
    def __init__(self, config):
        super().__init__(config)

    def _train_step(self, user_id, item_id, model: TowerModel, debias: BatchMixup_Debias, **kwargs):
        # Embedding
        query = model.construct_query(user_id)
        item_emb = model.item_encoder(item_id)
        # batch size
        B = user_id.shape[0]

        # 正样本的分数和修正项
        pos_score = torch.sum(query * item_emb, axis=1)
        log_pos_prob = debias.get_pop_bias(item_id)

        # Sample negative by mix-up
        sample_size = self.config['sample_size']
        assert 1 < sample_size < B, ValueError('The number of samples must be greater than 1 and smaller than '
                                               'batch_size')
        # 为每个正样本生成sample_size个beta分布 即对应sample_size个负样本
        M = beta(self.config['beta_alpha'], self.config['beta_alpha'], size=(B, sample_size, B))
        sample_weight = torch.softmax(torch.tensor(M), dim=-1)

        # (B x neg_sample x B) x (B x emb) = B x neg_sample x emb
        neg_item_emb = torch.matmul(sample_weight, item_emb)

        # B x neg_sample
        neg_score = torch.zeros(B, sample_size)
        for idx, user in enumerate(query):
            neg_score[idx] = torch.matmul(user, neg_item_emb[idx].T)

        if self.config['loss'] in ['CE']:
            # B x neg_sample
            log_neg_prob = debias(item_id, M)

            loss = model.loss(pos_score, log_pos_prob, neg_score, log_neg_prob)
        else:
            if pos_score.dim() < neg_score.dim():
                pos_score.unsqueeze_(-1)

            # r{uij} = r{ui} - r{uj},  B X neg_sample
            rat = pos_score - neg_score
            log_neg_prob = debias(item_id, M)

            loss = model.bpr_loss(rat, log_neg_prob)

        return loss


class Trainer_WithLast(Trainer):
    def __init__(self, config):
        super().__init__(config)
    
    def _train_step(self, user_id, item_id, model: TowerModel, debias: Base_Debias, last_id=None, **kwargs):
        query = model.construct_query(user_id)
        if last_id is None:
            items = item_id
        else:
            items = torch.cat([item_id, last_id], dim=-1)
        item_emb = model.item_encoder(items)

        B = user_id.shape[0]
        
        scores = torch.matmul(query, item_emb.T)
        log_pos_prob = debias(item_id)
        log_prob = debias(items)
        pos_rat = torch.diag(scores)
        
        log_neg_prob = log_prob.view(1, -1).repeat(B, 1)
        
        loss = model.loss(pos_rat, log_pos_prob, scores, log_neg_prob)

        return loss

    def _fit(self, model: TowerModel, debias: Base_Debias, train_loader: DataLoader, test_loader=None):
        num_epoch = self.config['epoch']
        optimizer = self.config_optimizers(model.parameters(), self.config['learning_rate'], self.config['weight_decay'])
        
        if self.config['steprl'] :
            scheduler = optim.lr_scheduler.StepLR(optimizer, self.config['step_size'], self.config['step_gamma'])

        for epoch in range(num_epoch):
            loss_ = 0.0
            
            for batch_idx, batch_data in enumerate(train_loader):
                model.train()
                debias.train()

                optimizer.zero_grad()

                user_id, item_id = batch_data
                user_id, item_id = user_id.to(self.device), item_id.to(self.device)

                
                if epoch == 0 and batch_idx == 0:
                    loss = self._train_step(user_id, item_id, model, debias)
                else:
                    loss = self._train_step(user_id, item_id, model, debias, last_id)
                
                last_id = item_id
                loss_ += loss.detach()
                loss.backward()
                optimizer.step()

            
            if self.config['steprl'] :
                scheduler.step()
            self.writer.add_scalar("Train/Loss", loss_/(batch_idx+1.0), epoch)
            self.logger.info('Epoch {}'.format(epoch))
            self.logger.info('***************Train loss {:.8f}'.format(loss_))

            if ((epoch % self.config['valid_interval']) == 0) or (epoch >= num_epoch - 1):
                with torch.no_grad():
                    out = self.evaluate(model, test_loader)

                for k in out.keys():
                    self.writer.add_scalar("Evaluate/{}".format(k), out[k], epoch)
                ress = (', ').join(["{} : {:.6f}".format(k, out[k]) for k in out.keys()])
                    
                self.logger.info('***************Eval_Res ' + ress)
        
            self.writer.flush()


class Trainer_Re_WithLast(Trainer_WithLast):
    def __init__(self, config):
        super().__init__(config)

    def _train_step(self, user_id, item_id, model: TowerModel, debias: Base_Debias, last_id=None, **kwargs):
        query = model.construct_query(user_id)
        if last_id is None:
            items = item_id
        else:
            items = torch.cat([item_id, last_id], dim=-1)
        item_emb = model.item_encoder(items)

        # B = user_id.shape[0]
        B = self.config['batch_size']

        # log_pos_prob = debias.get_pop_bias(item_id)
        log_pos_prob = - torch.log(self.item_num * torch.ones_like(item_id, dtype=torch.float, device=self.device))

        log_prob = debias.get_pop_bias(items)

        scores = torch.matmul(query, item_emb.T)
        pos_rat = torch.diag(scores)


        _, IndM, log_neg_prob = debias.resample(scores, log_prob, B)
        
        neg_rat = torch.gather(scores, 1, IndM)
        loss = model.loss(pos_rat, log_pos_prob, neg_rat, log_neg_prob)

        return loss
