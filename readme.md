# PreRadE
This repository contains code and models for my minor thesis: `PreRadE: An Evaluation Framework for Pretraining Tasks on Radiology Images and Reports`   

Note: codebase is a work in progress and will be updated ongoing (with potential for things to break). 

## Requirements
See env.yml

## Background
In a nutshell, this project aims to evaluate (model-agnostic) a range of common pretraining tasks used in current multimodal settings, and additionally conduct a preliminary investigation into novel methods.

## Implementation details:
- Pytorch lightning used as the experiment framework
- fairseq Detectron2 is used as the visual feature extraction pipeline (offline)
- huggingface transformers package used as the model base; specifically VisualBert for our experiments (although others should work with minor modifications) 
- Weights and biases used for experimental tracking, although again this can be modified as needed
- Tasks are emprically evaluated for multi-label classification AUC on radiological datasets

## Acknowledgements and Attribution:
We are grateful for the open source community without which we would not have been able to implement our framework. Aspects of our code are borrowed and/or modified from the following code and documentation sources (complete to the best of our knowledge):
- The transformers library (huggingface) for model implementations and pretrained checkpoints
- Detectron2 for visual feature extractor encoder model and pretrained checkpoint
- chhablani.gunjan@gmail.com for the [detectron2 notebook](https://colab.research.google.com/drive/1bLGxKdldwqnMVA5x4neY7-l_8fKGWQYI?usp=sharing#scrollTo=lmq8C39meEZX) to extract features from a dissected detectron2 model 
- https://github.com/YIKUAN8/Transformers-VQA for OpenI data preprocessing steps to obtain labels
- the VisualBert, UNITER and LXMert code repositories for various modelling components.  
- Sebastian Raschka for the mlextend statistical package and documentation for interpretation
- Weights and Biases for experiment tracking

## Data
- pretraining: MIMIC-CXR 
- finetuning: MIMIC-CXR, OpenI


## Important files


`pretrain.py`: Pretraining script, will require modification for logging & model/checkpoint load/save paths  
`data_paths.json`: File paths to datasets for pretraining, fine tuning and evaluating. Edit these with your processed data locations  
`src/data.py`: Contains the lightning DataModules for loading the text and visual features (from preprocessing steps below)  
`src/model.py`: Model code including pretraining and finetuning frameworks  
`src/tasks.py`: All pretext task code implementations contained within this file  
`src/utils.py`: Misc utils such as callbacks, logging, loading .tsv  
`src/parameters.py`: argparse arguments holds default values  

`preproc/extract_features.py`: Script to extract visual features from image data using Detectron2 mask-rcnn pretrained model  
`preproc/pp_utils.py`: Class and methods to implement mask-rcnn pretrained model for above script, with partial outputs for features  
`preproc/stratified_split.ipynb`: Preprocessing notebook to generate the report data in required format  

## Preprocessing

Refer to the provided a notebook for preprocess MIMIC-CXR reports and labels: `preproc/stratified_split.ipynb`  

To extract visual features from MIMIC-CXR-JPG with Detectron2 suitable for inputs to the model, first edit the `ROOT` value in `./preproc/extract_features.py`, then to run:
```python extract_features.py \
   --dataset mimic \
   --output [path_to_output.tsv] \
   --csv_file [path_to_processed_reports.csv] 
```

For extracting features from the OpenI dataset, first follow the preprocessing guidelines from [here](https://github.com/YIKUAN8/Transformers-VQA) (or the TieNet paper alternatively) and then run the above code swapping `mimic` for `openI`.


## Required mods

- customise your own logging either through wandb or another/none.  
- Load/save paths to checkpoint/model files  

By default the pretraining saves checkpoints in `[data_root]/checkpoints/[run_name]/pt_framework/` and models in `[data_root]/checkpoints/[run_name]/encoder/`


## Pre-training

E.g. To run the experimental setup with masked language modelling, masked feature regression, and image-text matching:

```bash pt.sh \
   --tasks mlm,mfr,itm \
   --load_model scratch \
   --max_seq_len 125 \
   --batch_size 64 \
   --project [wandb_name] \
   --steps 200000 \
   --lr 5e-5 \
```

## Fine-tuning & Evaluation

To fine tune a pretrained model using all mimic data, and evaluate on mimic/openI test set:
```bash ft.sh \
   --load_model [run_name] \
   --train mimic_100 \
   --test [mimic/openI] \
   --epochs 6\
```

To skip fine tuning and evaluate a saved model:
```bash ft.sh \
   --load_model [run_name] \
   --no_finetune True \
   --test [mimic/openI]
```


## Future Work

### Tasks
- Introduce pseudo-label task using offline cross-modal clustering
- Implement ELECTRA-style GAN corruption & discrimination task, using the simpler [uniform sampling method](arxiv.org/abs/2104.09694v1)
- Implement entity-level masking with word list generation

### Ablation studies
- Assess the robustness of the learned representations as the model is compressed through pruning (lottery ticket)

## License
mmRad is MIT licensed.

## Contact
If you have any questions, please contact Matthew Coleman `<mcol0029@student.monash.edu>` or create a Github issue.
