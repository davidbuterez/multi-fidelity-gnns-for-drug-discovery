from rdkit import Chem
from rdkit.Chem import Descriptors, AllChem
from rdkit.ML.Descriptors import MoleculeDescriptors

import pandas as pd
import numpy as np
import argparse
import os

from joblib import dump, load
from pathlib import Path
from sklearn import preprocessing
from sklearn.model_selection import PredefinedSplit, GridSearchCV
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier

import os

from multi_fidelity_modelling.src.reporting import get_metrics, get_cls_metrics


def remove_smiles_stereo(s):
    mol = Chem.MolFromSmiles(s)
    Chem.rdmolops.RemoveStereochemistry(mol)
    return (Chem.MolToSmiles(mol))


def save(main_path, model_type, rf_model, rf_preds, y_test, scaled=False, y_train_scaler=None, task_type='regression'):
    save_path_model = os.path.join(main_path, f'{model_type}')
    Path(save_path_model).mkdir(exist_ok=True, parents=True)

    dump(rf_model, os.path.join(save_path_model, 'rf_model.joblib'))
    np.save(os.path.join(save_path_model, 'y_pred.npy'), rf_preds)
    np.save(os.path.join(save_path_model, 'y_test.npy'), y_test)
    
    if not scaled:
        if task_type == 'regression':
            metrics = get_metrics(y_test, rf_preds)
        else:
            metrics = get_cls_metrics(y_test, rf_preds)
    else:
        if task_type == 'regression':
            metrics = get_metrics(y_test, y_train_scaler.inverse_transform(rf_preds.reshape(-1, 1)))
    np.save(os.path.join(save_path_model, 'metrics.npy'), metrics)

    return metrics


def main():
    parser = argparse.ArgumentParser(description='RF/SVR training on DR.')
    parser.add_argument('--path')
    parser.add_argument('--smiles-column')
    parser.add_argument('--DR-label')
    parser.add_argument('--SD-label')
    parser.add_argument('--SD-EMBS-label')
    parser.add_argument('--type')
    parser.add_argument('--save-path')
    args = parser.parse_args()
    argsdict = vars(args)

    assert argsdict['type'] in ['regression', 'classification']

    scoring = 'roc_auc' if argsdict['type'] == 'classification' else 'neg_mean_squared_error'
    RF = RandomForestClassifier if argsdict['type'] == 'classification' else RandomForestRegressor

    save_path = argsdict['save_path']
    Path(save_path).mkdir(exist_ok=True, parents=True)

    no_embs_path = argsdict['path']
    train_df = pd.read_csv(no_embs_path + '/train.csv')
    val_df = pd.read_csv(no_embs_path + '/validate.csv')
    test_df = pd.read_csv(no_embs_path + '/test.csv')

    print('Loaded data.')

    results = []

    train_smiles_no_stereo = [remove_smiles_stereo(smiles) for smiles in train_df[argsdict['smiles_column']].values]
    val_smiles_no_stereo = [remove_smiles_stereo(smiles) for smiles in val_df[argsdict['smiles_column']].values]
    test_smiles_no_stereo = [remove_smiles_stereo(smiles) for smiles in test_df[argsdict['smiles_column']].values]

    train_mols = [Chem.MolFromSmiles(s) for s in train_smiles_no_stereo]
    valid_mols = [Chem.MolFromSmiles(s) for s in val_smiles_no_stereo]
    test_mols = [Chem.MolFromSmiles(s) for s in test_smiles_no_stereo]

    fps_train = np.array([np.asarray(AllChem.GetMorganFingerprintAsBitVect(mol, 3, nBits=2048)) for mol in train_mols])
    fps_valid = np.array([np.asarray(AllChem.GetMorganFingerprintAsBitVect(mol, 3, nBits=2048)) for mol in valid_mols])
    fps_test = np.array([np.asarray(AllChem.GetMorganFingerprintAsBitVect(mol, 3, nBits=2048)) for mol in test_mols])

    print('Computed fingerprints.')

    y_train = train_df[argsdict['DR_label']].values
    y_val = val_df[argsdict['DR_label']].values
    y_test = test_df[argsdict['DR_label']].values

    num_cls = None
    if argsdict['type'] == 'classification':
        num_cls = len(set(np.concatenate((y_train, y_val, y_test)).tolist()))

    y_train_scaler = preprocessing.StandardScaler()
    y_train_scaler.fit_transform(np.reshape(np.array(train_df[argsdict['DR_label']]), (-1, 1)))

    y_train_scaled = y_train_scaler.transform(np.reshape(np.array(train_df[argsdict['DR_label']]), (-1, 1)))
    y_valid_scaled = y_train_scaler.transform(np.reshape(np.array(val_df[argsdict['DR_label']]), (-1, 1)))

    fps_train_val = np.concatenate([fps_train, fps_valid])
    y_train_val_scaled = np.reshape(np.concatenate([y_train_scaled, y_valid_scaled]), (-1,))
    y_train_val = np.reshape(np.concatenate([y_train, y_val]), (-1,))
    valid_fold = [-1] * fps_train.shape[0] + [0] * fps_valid.shape[0]
    ps = PredefinedSplit(valid_fold)

    y_train_scaled = y_train_scaled.ravel()
    y_valid_scaled = y_valid_scaled.ravel()

    save_path_rf_fps = os.path.join(save_path, 'RF/FP/')
    Path(save_path_rf_fps).mkdir(exist_ok=True, parents=True)


    random_grid = {'n_estimators': [50, 100, 150, 200, 250, 300, 500],
                'max_depth': [None, 25, 50, 100],
                'min_samples_leaf': [1, 2, 4],
                'min_samples_split': [2, 5, 10]
            }


    # ### RF NO SD, NO SCALER ###
    fps_grid_search = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    fps_grid_search.fit(fps_train_val, y_train_val)

    dump(fps_grid_search, os.path.join(save_path_rf_fps, 'fps_grid_search.joblib'))
    print('Grid search 1/12; DR only; FP best params: ', fps_grid_search.best_params_)
    
    rfr = RF(n_jobs=-1, **fps_grid_search.best_params_)
    rfr.fit(fps_train, y_train)
    rfr_preds = rfr.predict(fps_test)

    metrics = save(save_path_rf_fps, 'optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR only', 'RF FP hyp', None, *metrics))

    rfr = RF(n_jobs=-1)
    rfr.fit(fps_train, y_train)
    rfr_preds = rfr.predict(fps_test)

    metrics = save(save_path_rf_fps, 'default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR only', 'RF FP', None, *metrics))
    # ### RF NO SD, NO SCALER ###

    fps_train_sd = np.concatenate((fps_train, np.expand_dims(train_df[argsdict['SD_label']].values, axis=1)), axis=1)
    fps_val_sd = np.concatenate((fps_valid, np.expand_dims(val_df[argsdict['SD_label']].values, axis=1)), axis=1)
    fps_test_sd = np.concatenate((fps_test, np.expand_dims(test_df[argsdict['SD_label']].values, axis=1)), axis=1)

    fps_train_val_sd = np.concatenate([fps_train_sd, fps_val_sd])

    # ### RF WITH SD, NO SCALER ###
    fps_grid_search_sd = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    fps_grid_search_sd.fit(fps_train_val_sd, y_train_val)

    dump(fps_grid_search, os.path.join(save_path_rf_fps, 'fps_grid_search_sd.joblib'))
    print('Grid search 2/12; DR + SD; FP best params: ', fps_grid_search_sd.best_params_)

    rfr = RF(n_jobs=-1, **fps_grid_search_sd.best_params_)
    rfr.fit(fps_train_sd, y_train)
    rfr_preds = rfr.predict(fps_test_sd)

    metrics = save(save_path_rf_fps, 'sd_labels_optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD labels', 'RF FP hyp', None, *metrics))

    rfr = RF(n_jobs=-1)
    rfr.fit(fps_train_sd, y_train)
    rfr_preds = rfr.predict(fps_test_sd)

    metrics = save(save_path_rf_fps, 'sd_labels_default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD labels', 'RF FP', None, *metrics))
    # ### RF WITH SD, NO SCALER ###

    # ### RF NO SD, WITH SCALER ###
    if argsdict['type'] == 'regression':
        fps_grid_search = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        fps_grid_search.fit(fps_train_val, y_train_val_scaled)

        dump(fps_grid_search, os.path.join(save_path_rf_fps, 'fps_grid_search_scaled.joblib'))
        print('Grid search 3/12; DR only scaled; FP best params: ', fps_grid_search.best_params_)

        rfr = RF(n_jobs=-1, **fps_grid_search.best_params_)
        rfr.fit(fps_train, y_train_scaled)
        rfr_preds = rfr.predict(fps_test)

        metrics = save(save_path_rf_fps, 'scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR only', 'RF FP scaled hyp', None, *metrics))

        rfr = RF(n_jobs=-1)
        rfr.fit(fps_train, y_train_scaled)
        rfr_preds = rfr.predict(fps_test)

        metrics = save(save_path_rf_fps, 'scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR only', 'RF FP scaled', None, *metrics))
    # ### RF NO SD, WITH SCALER ###

    # ### RF WITH SD, WITH SCALER ###
    if argsdict['type'] == 'regression':
        fps_grid_search_sd = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        fps_grid_search_sd.fit(fps_train_val_sd, y_train_val_scaled)

        dump(fps_grid_search_sd, os.path.join(save_path_rf_fps, 'fps_grid_search_sd_scaled.joblib'))
        print('Grid search 4/12; DR + SD scaled; FP best params: ', fps_grid_search_sd.best_params_)

        rfr = RF(n_jobs=-1, **fps_grid_search_sd.best_params_)
        rfr.fit(fps_train_sd, y_train_scaled)
        rfr_preds = rfr.predict(fps_test_sd)

        metrics = save(save_path_rf_fps, 'sd_labels_scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD labels', 'RF FP scaled hyp', None, *metrics))

        rfr = RF(n_jobs=-1)
        rfr.fit(fps_train_sd, y_train_scaled)
        rfr_preds = rfr.predict(fps_test_sd)

        metrics = save(save_path_rf_fps, 'sd_labels_scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD labels', 'RF FP scaled', None, *metrics))
    # ### RF WITH SD, WITH SCALER ###

    ##### SD EMBS #####
    train_embs = np.array([np.array(arr.rstrip().lstrip().replace('[', '').replace(']', '').split(','), dtype=float) for arr in train_df[argsdict['SD_EMBS_label']].values])
    val_embs = np.array([np.array(arr.rstrip().lstrip().replace('[', '').replace(']', '').split(','), dtype=float) for arr in val_df[argsdict['SD_EMBS_label']].values])
    test_embs = np.array([np.array(arr.rstrip().lstrip().replace('[', '').replace(']', '').split(','), dtype=float) for arr in test_df[argsdict['SD_EMBS_label']].values])

    fps_train_sd_embs = np.concatenate((fps_train, train_embs), axis=1)
    fps_val_sd_embs = np.concatenate((fps_valid, val_embs), axis=1)
    fps_test_sd_embs = np.concatenate((fps_test, test_embs), axis=1)

    fps_train_val_sd_embs = np.concatenate([fps_train_sd_embs, fps_val_sd_embs])

    ### RF WITH SD EMBS, NO SCALER ###
    fps_grid_search_sd_embs = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    fps_grid_search_sd_embs.fit(fps_train_val_sd_embs, y_train_val)

    dump(fps_grid_search_sd_embs, os.path.join(save_path_rf_fps, 'fps_grid_search_sd_embs.joblib'))
    # print('Grid search 7/16; DR + SD EMBS; FP best params: ', fps_grid_search_sd_embs.best_params_)
    print('Grid search 5/12; DR + SD EMBS; FP best params: ', fps_grid_search_sd_embs.best_params_)

    rfr = RF(n_jobs=-1, **fps_grid_search_sd_embs.best_params_)
    rfr.fit(fps_train_sd_embs, y_train)
    rfr_preds = rfr.predict(fps_test_sd_embs)

    metrics = save(save_path_rf_fps, 'sd_embs_optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD embs', 'RF FP hyp', None, *metrics))

    rfr = RF(n_jobs=-1)
    rfr.fit(fps_train_sd_embs, y_train)
    rfr_preds = rfr.predict(fps_test_sd_embs)

    metrics = save(save_path_rf_fps, 'sd_embs_default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD embs', 'RF FP', None, *metrics))
    ### RF WITH SD EMBS, NO SCALER ###

    ### RF WITH SD EMBS, WITH SCALER ###
    if argsdict['type'] == 'regression':
        fps_grid_search_sd_embs = GridSearchCV(estimator = RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        fps_grid_search_sd_embs.fit(fps_train_val_sd_embs, y_train_val_scaled)

        dump(fps_grid_search_sd_embs, os.path.join(save_path_rf_fps, 'fps_grid_search_sd_embs_scaled.joblib'))
        # print('Grid search 8/16; DR + SD embs scaled; FP best params: ', fps_train_val_sd_embs.best_params_)
        print('Grid search 6/12; DR + SD embs scaled; FP best params: ', fps_grid_search_sd_embs.best_params_)

        rfr = RF(n_jobs=-1, **fps_grid_search_sd_embs.best_params_)
        rfr.fit(fps_train_sd_embs, y_train_scaled)
        rfr_preds = rfr.predict(fps_test_sd_embs)

        metrics = save(save_path_rf_fps, 'sd_embs_scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD embs', 'RF FP scaled hyp', None, *metrics))

        rfr = RF(n_jobs=-1)
        rfr.fit(fps_train_sd_embs, y_train_scaled)
        rfr_preds = rfr.predict(fps_test_sd_embs)

        metrics = save(save_path_rf_fps, 'sd_embs_scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD embs', 'RF FP scaled', None, *metrics))
    ### RF WITH SD EMBS, WITH SCALER ###

    ##### SD EMBS #####


    # PC
    # create the features and scale them
    all_feats=[x[0] for x in Descriptors._descList]
    calc = MoleculeDescriptors.MolecularDescriptorCalculator(all_feats)
    print()
    print('Using %i PhysChem features ' % (len(all_feats)))

    # compute descriptors
    train_descrs = [calc.CalcDescriptors(mol) for mol in train_mols]
    valid_descrs = [calc.CalcDescriptors(mol) for mol in valid_mols]
    test_descrs = [calc.CalcDescriptors(mol) for mol in test_mols]

    print('Computed PhysChem descriptors.')

    train_descrs = np.nan_to_num(np.array(train_descrs))
    test_descrs = np.nan_to_num(np.array(test_descrs))
    valid_descrs = np.nan_to_num(np.array(valid_descrs))

    scaler = preprocessing.StandardScaler()
    scaler.fit(train_descrs)

    X_train = scaler.transform(train_descrs)
    X_valid = scaler.transform(valid_descrs)
    X_test = scaler.transform(test_descrs)

    X_train_val = np.concatenate([X_train, X_valid])
    y_train_val = np.reshape(np.concatenate([y_train, y_val]), (-1,))
    valid_fold = [-1] * X_train.shape[0] + [0] * X_valid.shape[0]
    ps = PredefinedSplit(valid_fold)

    save_path_rf_pc = os.path.join(save_path, 'RF/PC/')
    Path(save_path_rf_pc).mkdir(exist_ok=True, parents=True)

    # ### RF NO SD, NO SCALER ###
    grid_search = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    grid_search.fit(X_train_val, y_train_val)

    dump(grid_search, os.path.join(save_path_rf_pc, 'pc_grid_search.joblib'))
    print('Grid search 7/12; DR only; PC best params: ', grid_search.best_params_)

    rfr = RF(n_jobs=-1, **grid_search.best_params_)
    rfr.fit(X_train, y_train)
    rfr_preds = rfr.predict(X_test)

    metrics = save(save_path_rf_pc, 'optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR only', 'RF PC hyp', None, *metrics))


    rfr = RF(n_jobs=-1)
    rfr.fit(X_train, y_train)
    rfr_preds = rfr.predict(X_test)

    metrics = save(save_path_rf_pc, 'default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR only', 'RF PC', None, *metrics))
    # ### RF NO SD, NO SCALER ###


    ### RF WITH SD, NO SCALER ###
    X_train_sd = np.concatenate((X_train, np.expand_dims(train_df[argsdict['SD_label']].values, axis=1)), axis=1)
    X_val_sd = np.concatenate((X_valid, np.expand_dims(val_df[argsdict['SD_label']].values, axis=1)), axis=1)
    X_test_sd = np.concatenate((X_test, np.expand_dims(test_df[argsdict['SD_label']].values, axis=1)), axis=1)

    X_train_val_sd = np.concatenate([X_train_sd, X_val_sd])

    grid_search_sd = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    grid_search_sd.fit(X_train_val_sd, y_train_val)

    dump(grid_search_sd, os.path.join(save_path_rf_pc, 'pc_grid_search_sd.joblib'))
    print('Grid search 8/12; DR + SD; PC best params: ', grid_search_sd.best_params_)

    rfr = RF(n_jobs=-1, **grid_search_sd.best_params_)
    rfr.fit(X_train_sd, y_train)
    rfr_preds = rfr.predict(X_test_sd)

    metrics = save(save_path_rf_pc, 'sd_labels_optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD labels', 'RF PC hyp', None, *metrics))


    rfr = RF(n_jobs=-1)
    rfr.fit(X_train_sd, y_train)
    rfr_preds = rfr.predict(X_test_sd)

    metrics = save(save_path_rf_pc, 'sd_labels_default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD labels', 'RF PC', None, *metrics))
    # ### RF WITH SD, NO SCALER ###

    # ### RF NO SD, WITH SCALER ###
    if argsdict['type'] == 'regression':
        grid_search = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        grid_search.fit(X_train_val, y_train_val_scaled)

        dump(grid_search, os.path.join(save_path_rf_pc, 'pc_grid_search_scaled.joblib'))
        print('Grid search 9/12; DR only; PC scaled best params: ', grid_search.best_params_)


        rfr = RF(n_jobs=-1, **grid_search.best_params_)
        rfr.fit(X_train, y_train_scaled)
        rfr_preds = rfr.predict(X_test)

        metrics = save(save_path_rf_pc, 'scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR only', 'RF PC scaled hyp', None, *metrics))


        rfr = RF(n_jobs=-1)
        rfr.fit(X_train, y_train_scaled)
        rfr_preds = rfr.predict(X_test)

        metrics = save(save_path_rf_pc, 'scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR only', 'RF PC scaled', None, *metrics))
    # ### RF NO SD, WITH SCALER ###


    # ### RF WITH SD, WITH SCALER ###
    if argsdict['type'] == 'regression':
        grid_search_sd = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        grid_search_sd.fit(X_train_val_sd, y_train_val_scaled)

        dump(grid_search_sd, os.path.join(save_path_rf_pc, 'pc_grid_search_scaled.joblib'))
        print('Grid search 10/12; DR + SD; PC scaled best params: ', grid_search_sd.best_params_)

        rfr = RF(n_jobs=-1, **grid_search_sd.best_params_)
        rfr.fit(X_train_sd, y_train_scaled)
        rfr_preds = rfr.predict(X_test_sd)

        metrics = save(save_path_rf_pc, 'sd_labels_scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD labels', 'RF PC scaled hyp', None, *metrics))

        rfr = RF(n_jobs=-1)
        rfr.fit(X_train_sd, y_train_scaled)
        rfr_preds = rfr.predict(X_test_sd)

        metrics = save(save_path_rf_pc, 'sd_labels_scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD labels', 'RF PC scaled', None, *metrics))
    # ### RF WITH SD, WITH SCALER ###


    ##### SD EMBS #####
    X_train_sd_embs = np.concatenate((X_train, train_embs), axis=1)
    X_val_sd_embs = np.concatenate((X_valid, val_embs), axis=1)
    X_test_sd_embs = np.concatenate((X_test, test_embs), axis=1)

    X_train_val_sd_embs = np.concatenate([X_train_sd_embs, X_val_sd_embs])

    ### RF WITH SD EMBS, NO SCALER ###
    grid_search_sd = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
    grid_search_sd.fit(X_train_val_sd_embs, y_train_val)

    dump(grid_search_sd, os.path.join(save_path_rf_pc, 'pc_grid_search_sd_embs.joblib'))
    # print('Grid search 15/16; DR + SD embs; PC best params: ', grid_search_sd.best_params_)
    print('Grid search 11/12; DR + SD embs; PC best params: ', grid_search_sd.best_params_)

    rfr = RF(n_jobs=-1, **grid_search_sd.best_params_)
    rfr.fit(X_train_sd_embs, y_train)
    rfr_preds = rfr.predict(X_test_sd_embs)

    metrics = save(save_path_rf_pc, 'sd_embs_optimised', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD embs', 'RF PC hyp', None, *metrics))


    rfr = RF(n_jobs=-1)
    rfr.fit(X_train_sd_embs, y_train)
    rfr_preds = rfr.predict(X_test_sd_embs)

    metrics = save(save_path_rf_pc, 'sd_embs_default', rfr, rfr_preds, y_test, task_type=argsdict['type'])
    results.append(('DR + SD embs', 'RF PC', None, *metrics))
    ### RF WITH SD EMBS, NO SCALER ###

    ### RF WITH SD EMBS, WITH SCALER ###
    if argsdict['type'] == 'regression':
        grid_search_sd = GridSearchCV(estimator=RF(n_jobs=-1), param_grid=random_grid, cv=ps, scoring=scoring, verbose=0)
        grid_search_sd.fit(X_train_val_sd_embs, y_train_val_scaled)

        dump(grid_search_sd, os.path.join(save_path_rf_pc, 'pc_grid_search_sd_embs_scaled.joblib'))
        # print('Grid search 16/16; DR + SD embs; PC scaled best params: ', grid_search_sd.best_params_)
        print('Grid search 12/12; DR + SD embs; PC scaled best params: ', grid_search_sd.best_params_)

        rfr = RF(n_jobs=-1, **grid_search_sd.best_params_)
        rfr.fit(X_train_sd_embs, y_train_scaled)
        rfr_preds = rfr.predict(X_test_sd_embs)

        metrics = save(save_path_rf_pc, 'sd_embs_scaled_optimised', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD embs', 'RF PC scaled hyp', None, *metrics))

        rfr = RF(n_jobs=-1)
        rfr.fit(X_train_sd_embs, y_train_scaled)
        rfr_preds = rfr.predict(X_test_sd_embs)

        metrics = save(save_path_rf_pc, 'sd_embs_scaled_default', rfr, rfr_preds, y_test, scaled=True, y_train_scaler=y_train_scaler, task_type=argsdict['type'])
        results.append(('DR + SD embs', 'RF PC scaled', None, *metrics))
    ### RF WITH SD EMBS, WITH SCALER ###
    ##### SD EMBS #####

    if argsdict['type'] == 'regression':
        df_results = pd.DataFrame(results, columns=['Augment', 'RF/SVR type', 'Embedding type', 'MAE', 'RMSE', 'Maximum error', 'R2', 'p-value'])
    else:
        df_results = pd.DataFrame(results, columns=['Augment', 'RF/SVR type', 'Embedding type', 'Predictions', 'Confusion matrix', 'AUROC', 'Classification report', 'MCC'])
        del df_results['Predictions']
    df_results.to_csv(os.path.join(argsdict['save_path'], 'metrics_RF') + '.csv', index=False)

if __name__ == "__main__":
    main()


