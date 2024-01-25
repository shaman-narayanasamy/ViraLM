from transformers import AutoTokenizer, AutoModelForSequenceClassification
from torch.utils.data import DataLoader
from datasets import load_dataset
from torch.utils.data import Dataset
from typing import Dict, Sequence
from dataclasses import dataclass
from torch.nn import Softmax
from Bio import SeqIO
import transformers
import numpy as np
import argparse
import torch
import csv
import re
import os


parser = argparse.ArgumentParser(description='ViraLM v1.0\nViraLM is a python library for identifying viruses from'
                                             'metagenomic data. ViraLM is based on the language model and rely on '
                                             'nucleotide information to make prediction.')
parser.add_argument('--input', type=str, help='name of the input file (fasta format)')
parser.add_argument('--output', type=str, help='output directory', default='result')
parser.add_argument('--len', type=int, help='predict only for sequences >= len bp (default: 500)', default=500)
parser.add_argument('--threshold', type=int, help='threshold for prediction (default: 0.5)', default=0.5)
inputs = parser.parse_args()

input_pth = inputs.input
output_pth = inputs.output
len_threshold = int(inputs.len)
score_threshold = float(inputs.threshold)
cache_dir = f'{output_pth}/cache'
model_pth = 'model'
filename = input_pth.rsplit('/')[-1].split('.')[0]

if score_threshold < 0.5:
    print('Threshold for prediction must be >= 0.5')
    exit(1)

if not os.path.exists(model_pth):
    print(f'Model directory {model_pth} missing or unreadable')
    exit(1)

if output_pth != '':
    if not os.path.isdir(output_pth):
        os.makedirs(output_pth)

if not os.path.isdir(cache_dir):
    os.makedirs(cache_dir)


def special_match(strg, search=re.compile(r'[^ACGT]').search):
    return not bool(search(strg))

def preprocee_data(input_pth, output_pth, len_threshold):
    frag_len = 2000
    filename = input_pth.rsplit('/')[-1].split('.')[0]
    f = open(f"{output_pth}/{filename}_temp.csv", "w")
    f.write(f'sequence,accession\n')
    for record in SeqIO.parse(input_pth, "fasta"):
        sequence = str(record.seq).upper()
        if len(sequence) < len_threshold:
            continue
        if len(sequence) >= frag_len:
            last_pos = 0
            for i in range(0, len(sequence)-frag_len+1, 2000):
                sequence1 = sequence[i:i + frag_len]
                if special_match(sequence1):
                    f.write(f'{sequence1},{f"{record.id}_{i}_{i+frag_len}"}\n')
                last_pos = i+frag_len
            if len(sequence) - last_pos >= 500:
                sequence1 = sequence[last_pos-0:]
                if special_match(sequence1):
                    f.write(f'{sequence1},{f"{record.id}_{last_pos - 0}_{len(record.seq)}"}\n')
        elif len(sequence) >= 500:
            if special_match(sequence):
                f.write(f'{sequence},{f"{record.id}_{0}_{0+len(sequence)}"}\n')
    f.close()


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self,
                 data_path: str,
                 tokenizer: transformers.PreTrainedTokenizer):

        super(SupervisedDataset, self).__init__()

        # load data from the disk
        with open(data_path, "r") as f:
            data = list(csv.reader(f))[1:]
        if len(data[0]) == 2:
            # data is in the format of [text, label]
            texts = [d[0] for d in data]
            labels = [int(d[1]) for d in data]

        output = tokenizer(
            texts,
            return_tensors="pt",
            padding="longest",
            max_length=tokenizer.model_max_length,
            truncation=True,
        )

        self.input_ids = output["input_ids"]
        self.attention_mask = output["attention_mask"]
        self.labels = labels

    def __len__(self):
        return len(self.input_ids)

    def __getitem__(self, i) -> Dict[str, str]:
        return dict(input_ids=self.input_ids[i], labels=self.labels[i])


@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""

    tokenizer: transformers.PreTrainedTokenizer

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        input_ids, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "accession"))
        input_ids = torch.nn.utils.rnn.pad_sequence(
            input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id
        )
        labels = labels
        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=input_ids.ne(self.tokenizer.pad_token_id),
        )

def tokenize_function(examples):
    return tokenizer(examples["sequence"], truncation=True)


preprocee_data(input_pth, output_pth, len_threshold)

model = AutoModelForSequenceClassification.from_pretrained(
        model_pth,
        num_labels=2,
        trust_remote_code=True,
        cache_dir=cache_dir,
    )

tokenizer = AutoTokenizer.from_pretrained(
        model_pth,
        model_max_length=512,
        padding_side="right",
        use_fast=True,
        trust_remote_code=True,
    )

data_collator = DataCollatorForSupervisedDataset(tokenizer=tokenizer)
test_dataset = load_dataset('csv', data_files={'test': f'{output_pth}/{filename}_temp.csv'}, cache_dir=cache_dir)
tokenized_datasets = test_dataset.map(tokenize_function, batched=True, batch_size=256, remove_columns=["sequence"])
tokenized_datasets = tokenized_datasets.with_format("torch")
test_loader = DataLoader(tokenized_datasets["test"], batch_size=256, collate_fn=data_collator)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

softmax = Softmax(dim=0)
model.eval()
result = {}
with torch.no_grad():
    for step, batch in enumerate(test_loader):
        labels = batch['labels']
        batch.pop('labels')
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        logits = outputs.logits.cpu().numpy()
        predictions = np.argmax(logits, axis=-1)

        for i in torch.arange(len(labels)):
            value = softmax(torch.tensor([logits[i][0], logits[i][1]])).tolist()
            segment_name = labels[i]
            seq_name = segment_name.rsplit('_', 2)[0]
            if seq_name not in result:
                result[seq_name] = []
            result[seq_name].append(value[1])

f = open(f'{output_pth}/result_{filename}.csv', 'w')
f.write(f'seq_name,prediction,virus_score\n')
for seq_name in result:
    f.write(f'{seq_name},')
    score = np.mean(result[seq_name])
    if score > score_threshold:
        f.write(f'virus,{score}\n')
    else:
        f.write(f'non-virus,{score}\n')
f.close()

for root, dirs, files in os.walk(f'{output_pth}', topdown=False):
    for name in files:
        if f'result_{filename}.csv' not in name:
            os.remove(os.path.join(root, name))
    for name in dirs:
        os.rmdir(os.path.join(root, name))