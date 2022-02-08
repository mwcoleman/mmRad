import os, json, random
import torch
import numpy as np
# from torch.nn.modules.normalization import LayerNorm
from torch.utils.data import DataLoader, Dataset, random_split
import pytorch_lightning as pl
import pandas as pd

from src.utils import load_tsv


class MMRadDM(pl.LightningDataModule):
    def __init__(self, args):
        
        super().__init__()

        self.save_hyperparameters(args)

        self.num_workers = os.cpu_count()
        self.g = torch.Generator()
        self.g.manual_seed(808)

    def prepare_data(self):
        # Called on 1 GPU only
        pass

    def setup(self, stage=None):
        # Called on every GPU
        self.test_size = 0  

        if stage=='fit' or stage is None:
            
            if self.hparams.dataset=='mscoco':
                self.train_dset = CocoDataset(self.hparams.data_path+'captions_train2017.json',
                        self.hparams.data_path+'img_features/mscoco-train_2017-custom.tsv', topk=self.hparams.topk)
                self.valid_dset = CocoDataset(self.hparams.data_path+'captions_val2017.json',
                        self.hparams.data_path+'img_features/mscoco-val_2017-custom.tsv', topk=self.hparams.val_topk)
            
            elif self.hparams.dataset=='mimic':
                # MIMIC set not split into train/val
                #txt_path, img_path, labels_path, use_captions='findings', topk=5120
                self.mimic_root = self.hparams.data_path
                self.txt_path = os.path.join(self.mimic_root, self.hparams.txt_path)
                self.img_path = os.path.join(self.mimic_root, self.hparams.img_path)
                self.label_path = os.path.join(self.mimic_root, 'labels', 'mimic-cxr-2.0.0-chexpert.csv.gz')
                
                mimic_data = MimicDataset(self.txt_path, self.img_path,
                                          topk=self.hparams.topk,
                                          binary_task=self.hparams.easy_classification)
                if self.hparams.valid_data is None:
                    print("No val data provided, splitting from train data")
                    train_set_size = int(len(mimic_data)*0.9)
                    valid_set_size = len(mimic_data) - train_set_size
                    self.train_dset, self.valid_dset = random_split(mimic_data, [train_set_size, valid_set_size],
                                                                    generator=self.g)
                else:
                    valid_path = os.path.join(self.mimic_root,self.hparams.valid_data)
                    self.train_dset = mimic_data
                    self.valid_dset = MimicDataset(self.txt_path, valid_path,
                                          topk=self.hparams.val_topk,
                                          binary_task=self.hparams.easy_classification)
                    
                self.num_classes = 1 if self.hparams.easy_classification else 13
                self.labelset = mimic_data.labelset
                self.train_size,self.valid_size = len(self.train_dset), len(self.valid_dset)
                print(f"Size of train / val / test splits: {self.train_size} / {self.valid_size} / {self.test_size}")
            
        if stage=='test' or stage is None:
             
            if self.hparams.test_data is None:
                pass
            else:
                if self.hparams.dataset=='mimic':
                    test_path = os.path.join(self.mimic_root,self.hparams.test_data)
                    print(f"Loading test data from {test_path}")
                    self.test_dset = MimicDataset(self.txt_path, test_path,
                                          binary_task=self.hparams.easy_classification)
                    self.test_size = len(self.test_dset)
                    
                    print(f"Finished loading.. Size of test set: {self.test_size}")

    def seed_worker(self,worker_id):
        worker_seed = torch.initial_seed() % 2**32
        np.random.seed(worker_seed)
        random.seed(worker_seed)


    def train_dataloader(self):
        dl = DataLoader(
            self.train_dset, batch_size=self.hparams.batch_size,
            shuffle=self.hparams.shuffle,
            drop_last=self.hparams.drop_last, pin_memory=True,
            num_workers=self.num_workers,
            worker_init_fn=self.seed_worker,
            generator=self.g,
        )
        return dl

    def val_dataloader(self):
        dl = DataLoader(
            self.valid_dset, batch_size=self.hparams.valid_batch_size,
            shuffle=False,
            drop_last=False, pin_memory=True,
            num_workers=self.num_workers,
            worker_init_fn=self.seed_worker,
            generator=self.g,
        )
        return dl
    
    def test_dataloader(self):
        if self.test_dset is not None:
            dl = DataLoader(
                self.test_dset, batch_size=self.hparams.valid_batch_size,
                shuffle=False,
                drop_last=False, pin_memory=True,
                num_workers=self.num_workers,
                worker_init_fn=self.seed_worker,
                generator=self.g,
            ) 
        else:
            print("Warning: Trying to load test dataloader, but no file specified.")
            dl = None
        return dl

class MimicDataset(Dataset):
    """Mimic-cxr dataset with extracted visual features,
    captions (from impressions), ID, view, ..."""
    def __init__(self, txt_path, img_path, 
                 topk=0, binary_task=False):
        super().__init__()
        self.binary_task = binary_task
        
        self.img_data = load_tsv(img_path, topk=topk)
        self.txt_data = pd.read_csv(txt_path)
        self.labelset = ['Atelectasis', 'Cardiomegaly', 'Consolidation',
                         'Edema', 'Enlarged Cardiomediastinum', 'Fracture',
                         'Lung Lesion', 'Lung Opacity',
                         'Pleural Effusion', 'Pleural Other', 'Pneumonia',
                         'Pneumothorax', 'Support Devices']


        # label_cats = self.label_data.columns[2:]
        

        # Filter img_ids to match loaded topk
        # This also filters on the PT or FT split of img features
        self.txt_data = self.txt_data[self.txt_data['dicom_id'].isin(self.img_data.keys())]
        self.txt_data.reset_index(inplace=True)

        # Get label data, default chexpert
        self.label_data = self.txt_data[self.labelset]
        # add labels - filter/sort on reports
        # self.txt_data = self.txt_data.merge(self.label_data, on='study_id', how='left')
        # self.labels_data = np.nan_to_num(np.asarray(self.txt_data[label_cats]))
        # self.labels_data[self.labels_data < 0] = 0
        if self.binary_task:
            # Any finding.
            self.label_data = (self.label_data.sum(axis=1)>0).astype(int)
        
    def __len__(self):
        return len(self.img_data)        
    
    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()
        
        # Produces a sample per txt sequence- image features are duplicated for each.        
        selected = self.txt_data.iloc[idx]
        img_data = self.img_data[selected['dicom_id']]
        caption = selected['report']
        # caption = selected['findings'] if self.use_captions == 'findings' else selected['impression']
        
        # label = self.label_data[self.label_data['study_id']==selected['study_id']]['labels'].values
        
        # TODO: if adding view to the input data,  preprocess to remove NaNs,
        #       else collate_fn bugs out         
        sample = {'txt': {'raw' : caption}, #'view': selected['view']},   Need to convert NaN to str to use this, or collate_fn bugs out.
                          
                  'img': {'id' : selected['dicom_id'], 
                          'features' : img_data['features'], 
                          'boxes' : img_data['boxes'],
                          'num_boxes' : img_data['num_boxes'], 
                          'img_h' : img_data['img_h'],
                          'img_w' : img_data['img_w']
                          },
                  'label': np.asarray(selected[self.labelset].astype(float))#self.label_data.iloc[idx]
                 }
        return sample

class CocoDataset(Dataset):
    """MS-COCO dataset captions only
    No transforms/process here"""
    def __init__(self, json_fp, img_ft_path, topk=5120):
        super().__init__()

        with open(json_fp) as f:
            self.metadata = json.load(f)
        # Dict of image fts' by id
        self.img_data = load_tsv(img_ft_path, topk=topk)
        # Add captions as duplicate tuples
        self.txt_data = [{'img_id':item['image_id'], 'caption':item['caption']} 
                          for item in self.metadata['annotations']]
        if topk != 0:
            # Filter img_ids to match loaded topk
            self.txt_data = [item for item in self.txt_data
                             if self.img_data.get(str(item['img_id']), 0) != 0]

    def __len__(self):
        return len(self.img_data)
    
    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()
        
        # Produces a sample per txt sequence- image features are duplicated for each.        
        img_id = str(self.txt_data[idx]['img_id'])
        caption = self.txt_data[idx]['caption']
        img_data = self.img_data[img_id]
        # Create nested
        sample = {'txt': {'raw' : caption}, 
                  'img': {'id' : img_id, 
                          'features' : img_data['features'], 
                          'boxes' : img_data['boxes'],
                          'num_boxes' : img_data['num_boxes'], 
                          'img_h' : img_data['img_h'],
                          'img_w' : img_data['img_w']
                          }
                 }
        return sample