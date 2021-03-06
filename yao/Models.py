import torch.nn as nn
import numpy as np
import torch
from sklearn.metrics import roc_auc_score

MAX_SEQ = 180
TAGS_NUM = 188
DROPOUT_RATE = 0.2

#######################################
seed_value = 42
torch.manual_seed(seed_value)
torch.cuda.manual_seed(seed_value)
torch.cuda.manual_seed_all(seed_value) # gpu vars
torch.backends.cudnn.deterministic = True  #needed
torch.backends.cudnn.benchmark = False
#######################################


class FFN(nn.Module):
    def __init__(self, state_size=200, forward_expansion=1, bn_size=MAX_SEQ-1, dropout=0.2):
        super(FFN, self).__init__()
        self.state_size = state_size
        
        self.lr1 = nn.Linear(state_size, forward_expansion * state_size)
        self.relu = nn.ReLU()
        self.bn = nn.BatchNorm1d(bn_size)
        self.lr2 = nn.Linear(forward_expansion * state_size, state_size)
        self.dropout = nn.Dropout(dropout)
        
    def forward(self, x):
        x = self.relu(self.lr1(x))
        x = self.bn(x)
        x = self.lr2(x)
        return self.dropout(x)


def future_mask(seq_length):
    future_mask = (np.triu(np.ones([seq_length, seq_length]), k = 1)).astype('bool')
    return torch.from_numpy(future_mask)


class TransformerBlock(nn.Module):
    def __init__(self, embed_dim, heads=8, dropout=DROPOUT_RATE, forward_expansion=1):
        super(TransformerBlock, self).__init__()
        self.multi_att = nn.MultiheadAttention(embed_dim=embed_dim, num_heads=heads, dropout=dropout)
        self.dropout = nn.Dropout(dropout)
        self.layer_normal = nn.LayerNorm(embed_dim)
        self.ffn = FFN(embed_dim, forward_expansion = forward_expansion, dropout=dropout)
        self.layer_normal_2 = nn.LayerNorm(embed_dim)
        

    def forward(self, value, key, query, att_mask):
        att_output, att_weight = self.multi_att(value, key, query, attn_mask=att_mask)
        att_output = self.dropout(self.layer_normal(att_output + value))
        att_output = att_output.permute(1, 0, 2) # att_output: [s_len, bs, embed] => [bs, s_len, embed]
        x = self.ffn(att_output)
        x = self.dropout(self.layer_normal_2(x + att_output))
        return x.squeeze(-1), att_weight
    

class Encoder(nn.Module):
    def __init__(self, n_skill, max_seq=100, embed_dim=128, dropout=DROPOUT_RATE, forward_expansion=1, num_layers=1, heads=8, pretrained_tags=None, max_tags_len=6):
        super(Encoder, self).__init__()
        self.n_skill, self.embed_dim, self.max_tags_len = n_skill, embed_dim, max_tags_len
        self.embedding = nn.Embedding(2 * n_skill + 1, embed_dim, padding_idx=0)
        self.pos_embedding = nn.Embedding(max_seq + 1, embed_dim, padding_idx=0)
        self.e_embedding = nn.Embedding(n_skill + 1, embed_dim, padding_idx=0)

        self.tags_embedding = nn.Embedding.from_pretrained(pretrained_tags, freeze=False, padding_idx=TAGS_NUM)
        self.question_encoder = nn.ModuleList([TransformerBlock(embed_dim, forward_expansion=forward_expansion) for _ in range(num_layers)])

        self.layers = nn.ModuleList([TransformerBlock(embed_dim, forward_expansion=forward_expansion) for _ in range(num_layers)])
        self.dropout = nn.Dropout(dropout)

    def fairseq_make_positions(self, tensor, padding_idx=0):
        mask = tensor.ne(padding_idx).int()
        return (
            torch.cumsum(mask, dim=1).type_as(mask) * mask
        ).long() + padding_idx
        
    def forward(self, x, question_ids, tag_ids):
        device = x.device

        pos_id = self.fairseq_make_positions(x)

        x = self.embedding(x)

        pos_x = self.pos_embedding(pos_id)
        
        x = self.dropout(x + pos_x)
        x = x.permute(1, 0, 2) # x: [bs, s_len, embed] => [s_len, bs, embed]

        e = self.e_embedding(question_ids)
        embedded_question_id = e.permute(1, 0, 2)

        embedded_tag = self.tags_embedding(tag_ids)
        embedded_tag = embedded_tag.sum(dim=2).permute(1, 0, 2).float()

        for layer in self.question_encoder:
            question_att_mask = future_mask(embedded_tag.size(0)).to(device)
            question_embeddings, question_att_weight = layer(embedded_question_id, embedded_tag, embedded_tag, att_mask=question_att_mask)
            question_embeddings = question_embeddings.permute(1, 0, 2)

        for layer in self.layers:
            att_mask = future_mask(question_embeddings.size(0)).to(device)
            x, att_weight = layer(question_embeddings, x, x, att_mask=att_mask)
            x = x.permute(1, 0, 2)
        x = x.permute(1, 0, 2)
        return x, att_weight, question_att_weight


class SAKTModel(nn.Module):
    def __init__(self, n_skill, max_seq=100, embed_dim=128, dropout=DROPOUT_RATE, forward_expansion=1, enc_layers=1, heads=8, pretrained_tags=None):
        super(SAKTModel, self).__init__()
        self.encoder = Encoder(n_skill, max_seq, embed_dim, dropout, forward_expansion, num_layers=enc_layers, pretrained_tags=pretrained_tags)
        self.pred = nn.Linear(embed_dim, 1)
        
    def forward(self, x, question_ids, tag_ids):
        x, att_weight, question_att_weight = self.encoder(x, question_ids, tag_ids)
        x = self.pred(x)
        return x.squeeze(-1), att_weight, question_att_weight


def train_fn(model, dataloader, optimizer, scheduler, criterion, device="cpu"):
    model.train()

    train_loss = []
    num_corrects = 0
    num_total = 0
    labels = []
    outs = []

    for item in dataloader:
        x = item[0].to(device).long()
        target_id = item[1].to(device).long()
        label = item[2].to(device).float()
        tags = item[3].to(device)
        target_mask = (target_id != 0)

        optimizer.zero_grad()
        output, att_weight, question_att_weight = model(x, target_id, tags)

        # exclude padding
        label = label[target_mask]
        output = output[target_mask]

        loss = criterion(output, label)
        loss.backward()
        optimizer.step()
        scheduler.step()
        train_loss.append(loss.item())

        # output = torch.masked_select(output, target_mask)
        # label = torch.masked_select(label, target_mask)
        pred = (torch.sigmoid(output) >= 0.5).long()
        
        num_corrects += (pred == label).sum().item()
        num_total += len(label)

        labels.extend(label.view(-1).data.cpu().numpy())
        outs.extend(output.view(-1).data.cpu().numpy())

    acc = num_corrects / num_total
    auc = roc_auc_score(labels, outs)
    loss = np.mean(train_loss)

    return loss, acc, auc

def valid_fn(model, dataloader, criterion, device="cpu"):
    model.eval()

    valid_loss = []
    num_corrects = 0
    num_total = 0
    labels = []
    outs = []

    for item in dataloader:
        x = item[0].to(device).long()
        target_id = item[1].to(device).long()
        label = item[2].to(device).float()
        tags = item[3].to(device)
        target_mask = (target_id != 0)

        output, att_weight, question_att_weight = model(x, target_id, tags)

        # exclude padding
        label = label[target_mask]
        output = output[target_mask]

        loss = criterion(output, label)
        valid_loss.append(loss.item())

        # output = torch.masked_select(output, target_mask)
        # label = torch.masked_select(label, target_mask)
        pred = (torch.sigmoid(output) >= 0.5).long()
        
        num_corrects += (pred == label).sum().item()
        num_total += len(label)

        labels.extend(label.view(-1).data.cpu().numpy())
        outs.extend(output.view(-1).data.cpu().numpy())

    acc = num_corrects / num_total
    auc = roc_auc_score(labels, outs)
    loss = np.mean(valid_loss)

    return loss, acc, auc