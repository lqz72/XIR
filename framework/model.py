import torch
import torch.nn as nn


class TowerModel(nn.Module):
    def __init__(self, user_num:int, item_num:int, emb_dim:int):
        super().__init__()
        item_num = item_num + 1  # (padding values)
        self.item_encoder = self._item_encoder(item_num, emb_dim)
    
    def _user_encoder(self):
        raise NotImplementedError
    
    def _item_encoder(self, item_num, emb_dim):
        return nn.Embedding(item_num, emb_dim)
    
    def scorer(self, query, items):
        # Inner product
        if query.size(0) == items.size(0):
            if query.dim() < items.dim():
                output = torch.bmm(items, query.view(*query.shape, 1))
                output = output.view(output.shape[:-1])
            else:
                output = torch.sum(query * items, dim=-1)
        else:
            output = torch.matmul(query, items.T)
        return output
    
    def construct_query(self):
        raise NotImplementedError
    
    def loss(self):
        raise NotImplementedError

    def forward(self, user_id, pos_items, neg_items):
        query = self.construct_query(user_id)
        pos_items_emb = self.item_encoder(pos_items)
        neg_items_emb = self.item_encoder(neg_items)
        return self.scorer(query, pos_items_emb), self.scorer(query, neg_items_emb)


class MFModel(TowerModel):
    def __init__(self, user_num: int, item_num: int, emb_dim: int):
        super().__init__(user_num, item_num, emb_dim)
        self.user_encoder = self._user_encoder(user_num, emb_dim)
    
    def _user_encoder(self, user_num, emb_dim):
        return nn.Embedding(user_num, emb_dim)
    
    def construct_query(self, user_id):
        return self.user_encoder(user_id)

    def loss(self, pos_score, log_pos_prob, neg_score, log_neg_prob):
        # pos_score : B
        # neg_score : B x neg_sample
        new_pos = pos_score - log_pos_prob.detach()  # score修正
        new_neg = neg_score - log_neg_prob.detach()
        if new_pos.dim() < new_neg.dim():
            new_pos.unsqueeze_(-1)
        partition = torch.cat([new_pos, new_neg], dim=-1)  # B x (B + 1)
        output = torch.logsumexp(partition, dim=-1, keepdim=True) - new_pos
        return torch.mean(output)

    def bpr_loss(self, rat, log_neg_prob):
        # rat B x neg_sample
        # log_neg_prob B x neg_sample
        new_rat = rat - log_neg_prob.detach()
        w = torch.softmax(new_rat, dim=-1)

        return torch.sum(w * torch.log(1.0 + torch.exp(-rat)))

    def loss_(self, pos_score, neg_score):
        if pos_score.dim() < neg_score.dim():
            pos_score.unsqueeze_(-1)
        partition = torch.cat([pos_score, neg_score], dim=-1)
        output = torch.logsumexp(partition, dim=-1, keepdim=True) - pos_score
        return torch.mean(output)


if __name__ == '__main__':
    a = torch.tensor([1,2,3], dtype=torch.float)
    a = torch.unsqueeze(a, -1)  # 3x1
    print(a)
    b = torch.tensor([[1,1,1], [2,2,2], [3,3,3]], dtype=torch.float) # 3x3
    c = torch.cat([a, b], dim=-1)
    print(c)
    d = torch.logsumexp(c, dim=-1, keepdim=True)
    print(d)