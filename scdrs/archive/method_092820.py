import scanpy as sc
import numpy as np
import scipy as sp
from skmisc.loess import loess
from statsmodels.stats.multitest import multipletests
from scipy.stats import rankdata
import pandas as pd
import time

def score_cell(data, 
               gene_list, 
               suffix='',
               ctrl_opt='mean_match',
               trs_opt='vst',
               bc_opt='use_ctrl',
               ctrlgene_list=None,
               n_ctrl=1,
               n_genebin=200,
               random_seed=0,
               verbose=False,
               copy=False,
               return_list=['trs_ep', 'trs_ez']):
    
    """Score cells based on the geneset 
    Fixit: I haven't thought of how to add gene_weight for computing ctrl trs. So for now, I suggest just set gene_weight=None
    
    Args:
        data (AnnData): 
            AnnData object
            adata.X should contain size-normalized log1p transformed count data
        gene_list (list): 
            gene list            
        suffix (str): 
            The name of the added cell-level annotations would be 
            'trs_'['', '_z', '_p', '_bhp']+suffix would be the name
        ctrl_opt (str): 
            Option for selecting the null geneset
            None: not using a null geneset
            'random': size matched random geneset
            'mean_match' size-and-mean-matched random geneset
            'mean_bvar_match': size-and-mean-and-bvar-matched random geneset. bvar means biological variance.
        trs_opt (str): 
            Option for computing TRS
            'mean': average over the genes in the gene_list
            'vst': weighted average with weights equal to 1/sqrt(technical_variance_of_logct)
            'inv_std': weighted average with weights equal to 1/std
        bc_opt (str):
            Option for cell-wise background correction
            None: no correction.
            'recipe_vision': normalize by cell-wise mean&var computed using all genes. 
            'empi': normalize by cell-wise mean&var stratified by mean bins.
        ctrlgene_list (list):
            The list of null genes to use
        n_ctrl (int): 
            Number of control genesets
        n_genebin (int): 
            Number of gene bins (to divide the genes by expression)
            Only useful if ctrl_opt is not None
        random_seed (int):
            Random seed
        copy (bool):
            If to make copy of the AnnData object
        return_list (list): 
            Items to return

    Returns:
        adata (AnnData): 
            Add columns to data.obs as specified by return_list
    """
    
    np.random.seed(random_seed)
    
    adata = data.copy() if copy else data
    
    # Check options
    ctrl_opt_list = [None, 'given', 'random', 'mean_match', 'mean_bvar_match']
    trs_opt_list = ['mean', 'vst', 'inv_std']
    bc_opt_list = [None, 'recipe_vision', 'empi']
    if ctrl_opt not in ctrl_opt_list:
        raise ValueError('# score_cell: ctrl_opt not in [%s]'%', '.join([str(x) for x in ctrl_opt_list]))
    if trs_opt not in trs_opt_list:
        raise ValueError('# score_cell: trs_opt not in [%s]'%', '.join([str(x) for x in trs_opt_list]))
    if bc_opt not in bc_opt_list:
        raise ValueError('# score_cell: bc_opt not in [%s]'%', '.join([str(x) for x in bc_opt_list]))
        
    if verbose:
        print('# score_cell: suffix=%s, ctrl_opt=%s, trs_opt=%s, bc_opt=%s'%(suffix, ctrl_opt, trs_opt, bc_opt))
        print('# score_cell: n_ctrl=%d, n_genebin=%d'%(n_ctrl, n_genebin))
        
    # Gene-wise statistics
    var_set = set(['mean','var','var_tech'])
    obs_set = set(['mean','var'])
    if (len(var_set-set(adata.var.columns))>0) | (len(obs_set-set(adata.obs.columns))>0):
        if verbose: print('# score_cell: recompute statistics using compute_stats')
        compute_stats(adata)
        
    df_gene = pd.DataFrame(index=adata.var_names)
    df_gene['gene'] = df_gene.index
    df_gene['mean'] = adata.var['mean']
    df_gene['var'] = adata.var['var'].values
    df_gene['tvar'] = adata.var['var_tech'].values
    df_gene['bvar'] = df_gene['var'].values - df_gene['tvar'].values
    df_gene.drop_duplicates(subset='gene', inplace=True)
        
    # Update gene_list  
    n_gene_old = len(gene_list)
    gene_list = list(set(df_gene['gene'].values) & set(gene_list))
    if verbose: 
        print('# score_cell: %-15s %-15s %-20s'
              %('trait geneset,', '%d/%d genes,'%(len(gene_list),n_gene_old),
                'mean_exp=%0.2e'%df_gene.loc[gene_list, 'mean'].mean()))
    
    # Select control genes: put all methods in _select_ctrl_geneset
    dic_ctrl_list = _select_ctrl_geneset(df_gene, gene_list, ctrl_opt, ctrlgene_list,
                                         n_ctrl, n_genebin, random_seed, verbose)
    
    # Compute TRS: put all methods in _compute_trs
    dic_trs = {}
    dic_trs['trs'] = _compute_trs(adata, gene_list, trs_opt)
    for i_list in dic_ctrl_list.keys(): 
        dic_trs['trs_ctrl%d'%i_list] = _compute_trs(adata, dic_ctrl_list[i_list], trs_opt)
        
    # Correct cell-specific and geneset-specific background: put all methods in _correct_background
    _correct_background(adata, dic_trs, bc_opt)
    
    # Get p-value
    if 'trs_tp' in return_list:
        dic_trs['trs_tp'] = 1 - sp.stats.norm.cdf(dic_trs['trs_z'])
    if len(dic_ctrl_list.keys())>0:
        v_ctrl_trs_z = []
        for i_list in dic_ctrl_list.keys(): 
            v_ctrl_trs_z += list(dic_trs['trs_ctrl%d_z'%i_list])
        dic_trs['trs_ep'] = get_p_from_empi_null(dic_trs['trs_z'],v_ctrl_trs_z)  
        if 'trs_ez' in return_list:
            dic_trs['trs_ez'] = -sp.stats.norm.ppf(dic_trs['trs_ep'])
            dic_trs['trs_ez'] = dic_trs['trs_ez'].clip(min=-10,max=10)
    
    for term in return_list:
        if term in dic_trs.keys():
            adata.obs['%s%s'%(term,suffix)] = dic_trs[term].copy()
        else:
            print('# score_cell: %s not computed'%term)
            
    return adata if copy else None

def _select_ctrl_geneset(input_df_gene, gene_list, ctrl_opt, ctrlgene_list, 
                         n_ctrl, n_genebin, random_seed, verbose):
    
    """Subroutine for score_cell, select control genesets 
    
    Args:
        input_df_gene (pd.DataFrame): (adata.shape[1], n_statistics)
            Gene-wise statistics
        gene_list (list): 
            gene list 
        ctrl_opt (str): 
            Option for selecting the null geneset
            None: not using a null geneset
            'random': size matched random geneset
            'mean_match' size-and-mean-matched random geneset
            'mean_bvar_match': size-and-mean-and-bvar-matched random geneset. bvar means biological variance.
        ctrlgene_list (list):
            The list of null genes to use
        n_ctrl (int): 
            Number of control genesets
        n_genebin (int): 
            Number of gene bins (to divide the genes by expression)
            Only useful if ctrl_opt is not None
        random_seed (int):
            Random seed           

    Returns:
        dic_ctrl_list (dictionary of lists): 
            dic_ctrl_list[i]: the i-th control gene list (a list)
            
    """
    
    np.random.seed(random_seed)
    dic_ctrl_list = {}
    df_gene = input_df_gene.copy()
    
    if ctrl_opt=='given':
        dic_ctrl_list[0] = ctrlgene_list
        
    if ctrl_opt=='random':
        for i_list in np.arange(n_ctrl):
            ind_select = np.random.permutation(df_gene.shape[0])[:len(gene_list)]
            dic_ctrl_list[i_list] = list(df_gene['gene'].values[ind_select])
        
    if ctrl_opt=='mean_match':
        # Divide genes into bins based on their rank of mean expression
        df_gene['qbin'] = pd.qcut(df_gene['mean'], q=n_genebin, labels=False)
        df_gene_bin = df_gene.groupby('qbin').agg({'gene':set})
        gene_list_as_set = set(gene_list)
        for i_list in np.arange(n_ctrl):
            dic_ctrl_list[i_list] = []
            for bin_ in df_gene_bin.index:
                n_gene_in_bin = len(df_gene_bin.loc[bin_,'gene'] & gene_list_as_set)
                v_gene_bin = np.array(list(df_gene_bin.loc[bin_, 'gene'] - gene_list_as_set))
                if (n_gene_in_bin>0) & (v_gene_bin.shape[0]>0):
                    ind_select = np.random.permutation(v_gene_bin.shape[0])[0:n_gene_in_bin]
                    dic_ctrl_list[i_list] += list(v_gene_bin[ind_select])
                    
    if ctrl_opt=='mean_bvar_match':
        # Divide genes into bins based on their rank of mean expression
        df_gene['mean_qbin'] = pd.qcut(df_gene['mean'], q=np.ceil(np.sqrt(n_genebin)), labels=False)
        df_gene['qbin'] = ''
        for bin_ in set(df_gene['mean_qbin']):
            ind_select = (df_gene['mean_qbin']==bin_)
            df_gene.loc[ind_select,'qbin'] = ['%d.%d'%(bin_,x) for x in 
                                              pd.qcut(df_gene.loc[ind_select,'bvar'],
                                                      q=np.ceil(np.sqrt(n_genebin)), labels=False)]
        df_gene_bin = df_gene.groupby('qbin').agg({'gene':set})
        gene_list_as_set = set(gene_list)
        for i_list in np.arange(n_ctrl):
            dic_ctrl_list[i_list] = []
            for bin_ in df_gene_bin.index:
                n_gene_in_bin = len(df_gene_bin.loc[bin_,'gene'] & gene_list_as_set)
                v_gene_bin = np.array(list(df_gene_bin.loc[bin_, 'gene'] - gene_list_as_set))
                if (n_gene_in_bin>0) & (v_gene_bin.shape[0]>0):
                    ind_select = np.random.permutation(v_gene_bin.shape[0])[0:n_gene_in_bin]
                    dic_ctrl_list[i_list] += list(v_gene_bin[ind_select])
                    
    if verbose:
        for i_list in dic_ctrl_list.keys():
            print('# score_cell: %-15s %-15s %-20s'
                  %('ctrl%d geneset,'%i_list, '%d genes,'%len(dic_ctrl_list[i_list]),
                    'mean_exp=%0.2e'%df_gene.loc[dic_ctrl_list[i_list], 'mean'].mean()))
                    
    return dic_ctrl_list

def _compute_trs(adata, gene_list, trs_opt):
    """Compute TRS
    
    Args:
        adata (AnnData): 
            adata.X should contain size-normalized log1p transformed count data
        gene_list (list): 
            List of genes used for scoring
        trs_opt (str): 
            Option for computing TRS
            'mean': average over the genes in the gene_list
            'vst': weighted average with weights equal to 1/sqrt(technical_variance_of_logct)
            'inv_std': weighted average with weights equal to 1/std
            
    Returns:
        v_trs (np.ndarray): (n_cell,)
            Raw TRS
    """
    
    if trs_opt=='mean':
        v_trs_weight = np.ones(len(gene_list)) / len(gene_list)
        temp_v = adata[:, gene_list].X.dot(v_trs_weight)
        v_trs = np.array(temp_v, dtype=np.float64).reshape([-1])
    
    if trs_opt=='vst':
        v_trs_weight = 1 / np.sqrt(adata.var.loc[gene_list,'var_tech'].values.clip(min=1e-2))
        v_trs_weight /= v_trs_weight.sum()
        temp_v = adata[:, gene_list].X.dot(v_trs_weight)
        v_trs = np.array(temp_v, dtype=np.float64).reshape([-1])
            
    if trs_opt=='inv_std':
        v_trs_weight = 1 / np.sqrt(adata.var.loc[gene_list,'var'].values.clip(min=1e-2))
        v_trs_weight /= v_trs_weight.sum()
        temp_v = adata[:, gene_list].X.dot(v_trs_weight)
        v_trs = np.array(temp_v, dtype=np.float64).reshape([-1])    
        
    return v_trs
    
def _correct_background(adata, dic_trs, bc_opt):
    """Cell-wise and gene-wise background correction
    
    Args:
        adata (AnnData): 
            adata.X should contain size-normalized log1p transformed count data
        dic_trs (dictionary of np.ndarray): each has dimension (n_cell,)
            Trait TRS and control TRSs
        bc_opt (str):
            Option for cell-wise background correction
            None: no correction.
            'recipe_vision': normalize by cell-wise mean&var computed using all genes. 
            'empi': normalize by cell-wise mean&var stratified by mean bins.
            
    Returns:
        Add trs_z and trs_ctrl%d_z to dic_trs (np.ndarray): (n_cell,)
            Normalized TRS z_score
    """
    
    # Cell-specific background correction
    trs_ctrl_list = [x for x in dic_trs if 'ctrl' in x]
    v_mean,v_std = adata.obs['mean'].values,np.sqrt(adata.obs['var'].values)
    n_cell = adata.shape[0]
    
    if bc_opt is None:
        for trs_name in ['trs']+trs_ctrl_list:
            dic_trs['%s_z'%trs_name] = dic_trs[trs_name]
    
    if bc_opt == 'recipe_vision':
        for trs_name in ['trs']+trs_ctrl_list:
            dic_trs['%s_z'%trs_name] = (dic_trs[trs_name] - v_mean) / v_std
            
    if bc_opt == 'empi':
        # Using TRSs to estimate empirical cell-specific background TRS mean&std
        if len(trs_ctrl_list)==0:
            raise ValueError('# score_cell: bc_opt=%s only works when n_ctrl>0'%bc_opt)
            
        df_cell = None
        for trs_name in ['trs']+trs_ctrl_list:
            temp_df = pd.DataFrame()
            temp_df['mean'] = v_mean
            temp_df['trs'] = dic_trs[trs_name]
            if df_cell is None:
                df_cell = temp_df.copy()
            else:
                df_cell = pd.concat([df_cell, temp_df], axis=0)
        df_cell['qbin'] = pd.qcut(df_cell['mean'], q=100, labels=False)
        
        # bin-specific mean and var
        dic_bin_mean = {x:df_cell.loc[df_cell['qbin']==x, 'trs'].values.mean() for x in set(df_cell['qbin'])}
        dic_bin_std = {x:df_cell.loc[df_cell['qbin']==x, 'trs'].values.std() for x in set(df_cell['qbin'])}
        v_mean_ctrl = np.array([dic_bin_mean[x] for x in df_cell['qbin'][:n_cell]])
        v_std_ctrl = np.array([dic_bin_std[x] for x in df_cell['qbin'][:n_cell]]).clip(min=1e-8)
        
        for trs_name in ['trs']+trs_ctrl_list:
            dic_trs['%s_z'%trs_name] = (dic_trs[trs_name] - v_mean_ctrl)/v_std_ctrl
        
    # Z-score each geneset (across cells)
    for trs_name in ['trs']+trs_ctrl_list:
        dic_trs['%s_z'%trs_name] = (dic_trs['%s_z'%trs_name] - dic_trs['%s_z'%trs_name].mean()) / dic_trs['%s_z'%trs_name].std()
    
    # Set cells with TRS=0 to the minimum TRS z-score value
    trs_min = dic_trs['trs_z'].min()
    for trs_name in trs_ctrl_list:
        trs_min = min(trs_min, dic_trs['%s_z'%trs_name].min())
    dic_trs['trs_z'][dic_trs['trs']==0] = trs_min
    for trs_name in trs_ctrl_list:
        dic_trs['%s_z'%trs_name][dic_trs[trs_name]==0] = trs_min+1e-8
    return
    

def get_sparse_var(sparse_X, axis=0):
    """
    Compute mean and var of a sparse matrix. 
    """   
    
    v_mean = sparse_X.mean(axis=axis)
    v_mean = np.array(v_mean).reshape([-1])
    v_var = sparse_X.power(2).mean(axis=axis)
    v_var = np.array(v_var).reshape([-1])
    v_var = v_var - v_mean**2
    
    return v_mean,v_var

def compute_stats(data, copy=False):
    """
    Precompute mean for each gene and mean&var for each cell
    """
    # Gene-wise statistics
    adata = data.copy() if copy else data
    adata.var['mean'],adata.var['var'] = get_sparse_var(adata.X, axis=0)
    
    # Get the mean and var for the size-factor-normalized counts
    # It is highly correlated to the non-size-factor-normalized counts
    temp_X = adata.X.copy().expm1() # exp(X)-1 to get ct matrix from logct
    adata.var['ct_mean'],adata.var['ct_var'] = get_sparse_var(temp_X, axis=0)
    del temp_X
    
    # Borrowed from scanpy _highly_variable_genes_seurat_v3
    not_const = adata.var['ct_var'].values>0
    estimat_var = np.zeros(adata.shape[1], dtype=np.float64)
    y = np.log10(adata.var['ct_var'].values[not_const])
    x = np.log10(adata.var['ct_mean'].values[not_const])
    model = loess(x, y, span=0.3, degree=2)
    model.fit()
    estimat_var[not_const] = model.outputs.fitted_values
    adata.var['ct_var_tech'] = 10**estimat_var
    # Recipe from Frost Nucleic Acids Research 2020
    adata.var['var_tech'] = adata.var['var']*adata.var['ct_var_tech']/adata.var['ct_var']
    adata.var.loc[adata.var['var_tech'].isna(),'var_tech'] = 0
    
    # Cell-wise statistics
    adata.obs['mean'],adata.obs['var'] = get_sparse_var(adata.X, axis=1)
    
    return adata if copy else None

def get_p_from_empi_null(v_t,v_t_null):
    """Compute p-value from empirical null
    For score T and a set of null score T_1,...T_N, the p-value is 
        
        p=1/(N+1) * [1 + \Sigma_{i=1}^N 1_{ (T_i \geq T) }]
        
    If T, T1, ..., T_N are i.i.d. variables following a null distritbuion, 
    then p is super-uniform. 
    
    The naive algorithm is N^2. Here we provide an O(N log N) algorithm to 
    compute the p-value for each of the N elements in v_t
    
    Args:
        v_t (M,): np.ndarray
            The observed score.
        v_t_null (N,): np.ndarray
            The null score.

    Returns:
        v_p: (M,): np.ndarray
            P-value for each element in v_t
    """
    
    v_t = np.array(v_t)
    v_t_null = np.array(v_t_null)
    
    v_t_null = np.sort(v_t_null)    
    v_pos = np.searchsorted(v_t_null, v_t, side='left')
    v_p = (v_t_null.shape[0]-v_pos+1)/(v_t_null.shape[0]+1)
    return v_p

##############################################################################
################################## Old code ##################################
##############################################################################

def score_cell_081520(data, 
               gene_list, 
               suffix='',
               flag_correct_background=False,
               verbose=True,
               copy=False):
    
    """score cells based on the geneset 
    
    Args:
        data (AnnData): AnnData object
            adata.X should contain size-normalized log1p transformed count data
        gene_list (list): gene list 
        suffix (str): 'trs_'+suffix+['', '_z', '_p', '_bhp'] would be the name
        flag_correct_background (bool):
            If normalize for background mean and std. If True, normalize by 
            score = (score - mean)/std
        tissue (str): 'all' or one of the facs or droplet tissues
        

    Returns:
        adata (AnnData): Combined data for FACS and droplet
    """
    
    adata = data.copy() if copy else data
    
    gene_list_overlap = list(set(adata.var_names) & set(gene_list))
    if verbose:
        print('# score_cell: %d/%d gene_list genes also in adata'
              %(len(gene_list), len(gene_list_overlap)))
        print('# score_cell: suffix=%s, flag_correct_background=%s'
              %(suffix, flag_correct_background))
        
    trs_name = 'trs_%s'%suffix
    if trs_name in adata.obs.columns:
        print('# score_cell: overwrite original %s in adata.obs.columns'
              %trs_name)
        
    adata.obs[trs_name] = adata[:, gene_list_overlap].X.mean(axis=1)
    
    if flag_correct_background:
        v_mean,v_var = get_sparse_var(adata.X, axis=1)
        v_std = np.sqrt(v_var)
        adata.obs[trs_name] = (adata.obs[trs_name] - v_mean) / v_std * \
                                np.sqrt(len(gene_list_overlap))
        
    # Add z_score, p_value, and fdr 
    temp_v = adata.obs[trs_name].values
    adata.obs['%s_z'%trs_name] = (temp_v - temp_v.mean())/ temp_v.std()
    adata.obs['%s_p'%trs_name] = 1 - sp.stats.norm.cdf(adata.obs['%s_z'%trs_name].values)
    adata.obs['%s_bhp'%trs_name] = multipletests(adata.obs['%s_p'%trs_name].values,
                                                 method='fdr_bh')[1]
    
    return adata if copy else None


def score_cell_kangcheng_072920(data, 
               gene_list, 
               suffix='',
               flag_correct_background=False,
               flag_specific_expressed=False,
               verbose=True,
               copy=False):
    
    """score cells based on the geneset 
    
    Args:
        data (AnnData): AnnData object
            adata.X should contain size-normalized log1p transformed count data
        gene_list (list): gene list 
        suffix (str): 'trs_'+suffix+['', '_z', '_p', '_bhp'] would be the name
        flag_correct_background (bool):
            If normalize for background mean and std per_cell. If True, normalize by 
            score = (score - mean)/std, where mean and std is calculated within each cell
        flag_specific_expressed (bool):
            Whether transform gene expression to identify specific expressed genes.
            If True, for each gene, normalize score = (score - mean) / std, where mean and 
            std is calculated across the cells when calculating the TRS score, 
        tissue (str): 'all' or one of the facs or droplet tissues
        

    Returns:
        adata (AnnData): Combined data for FACS and droplet
    """
    
    adata = data.copy() if copy else data
    
    gene_list_overlap = list(set(adata.var_names) & set(gene_list))
    if verbose:
        print('# score_cell: %d/%d gene_list genes also in adata'
              %(len(gene_list), len(gene_list_overlap)))
        print('# score_cell: suffix=%s, flag_correct_background=%s, flag_specific_expressed=%s'
              %(suffix, flag_correct_background, flag_specific_expressed))
        
    trs_name = 'trs_%s'%suffix
    if trs_name in adata.obs.columns:
        print('# score_cell: overwrite original %s in adata.obs.columns'
              %trs_name)
        
    adata.obs[trs_name] = adata[:, gene_list_overlap].X.mean(axis=1)
    
    if flag_correct_background:
        cell_mean,cell_var = get_sparse_var(adata.X, axis=1)
        cell_std = np.sqrt(cell_var)
        # reshape to (1, #cells) vector
        cell_mean = cell_mean[:, np.newaxis]
        cell_std = cell_std[:, np.newaxis]
    
    gwas_mat = adata[:, gene_list_overlap].X
    if flag_correct_background:
        # normalize for each cell
        gwas_mat = (gwas_mat - cell_mean) / cell_std
        
    if flag_specific_expressed:
        # normalize for each gene
        gene_mean, gene_std = np.mean(gwas_mat, axis=0), np.std(gwas_mat, axis=0)
        gwas_mat = (gwas_mat - gene_mean) / gene_std
    
    adata.obs[trs_name] = gwas_mat.mean(axis=1)
        
    # Add z_score, p_value, and fdr 
    temp_v = adata.obs[trs_name].values
    adata.obs['%s_z'%trs_name] = (temp_v - temp_v.mean())/ temp_v.std()
    adata.obs['%s_p'%trs_name] = 1 - sp.stats.norm.cdf(adata.obs['%s_z'%trs_name].values)
    adata.obs['%s_bhp'%trs_name] = multipletests(adata.obs['%s_p'%trs_name].values,
                                                 method='fdr_bh')[1]
    
    return adata if copy else None

def gearys_c(adata, val_obs, prefix, stratify_obs=None, copy=False):
    """
    Interface of computing Geary's C statistics
    
    Args:
        adata: Anndata object
        val_obs: the obs name to calculate this statistics
        prefix: the name will be `prefix`_gearys_C
        stratify_obs: Calculate the statistics using `stratify_obs` obs column,
            must be a categorical variable
    """
    
    adata = adata.copy() if copy else adata
    if stratify_obs is not None:
        assert adata.obs[stratify_obs].dtype.name == 'category', \
            "`stratify_obs` must correspond to a Categorical column"
        categories = adata.obs[stratify_obs].unique()
        all_c_stats = np.zeros(adata.shape[0])
        for cat in categories:
            s_index = adata.obs[stratify_obs] == cat
            all_c_stats[s_index] = _gearys_c(adata[s_index], adata[s_index].obs[val_obs])
        
    else:
        all_c_stats = _gearys_c(adata, adata.obs[val_obs])
    
    gearys_C_name = prefix + '_gearys_C'
    if gearys_C_name in adata.obs.columns:
        print('# gearys_c: overwrite original %s in adata.obs.columns'
              %gearys_C_name)
    adata.obs[gearys_C_name] = all_c_stats
    # adata.obs[gearys_C_name] = adata.obs[gearys_C_name].astype('category')

    return adata if copy else None

    
def _gearys_c(adata, vals):
    """Compute Geary's C statistics for an AnnData
    Adopted from https://github.com/ivirshup/scanpy/blob/metrics/scanpy/metrics/_gearys_c.py
    
    C =
        \frac{
            (N - 1)\sum_{i,j} w_{i,j} (x_i - x_j)^2
        }{
            2W \sum_i (x_i - \bar{x})^2
        }
    
    Args:
        adata (AnnData): AnnData object
            adata.obsp["Connectivities] should contain the connectivity graph, 
            with shape `(n_obs, n_obs)`
        vals (Array-like):
            Values to calculate Geary's C for. If one dimensional, should have
            shape `(n_obs,)`. 
    Returns:
        C: the Geary's C statistics
    """
    graph = adata.obsp["connectivities"]
    assert graph.shape[0] == graph.shape[1]
    graph_data = graph.data.astype(np.float_, copy=False)
    assert graph.shape[0] == vals.shape[0]
    assert(np.ndim(vals) == 1)
    
    W = graph_data.sum()
    N = len(graph.indptr) - 1
    vals_bar = vals.mean()
    vals = vals.astype(np.float_)
    
    # numerators
    total = 0.0
    for i in range(N):
        s = slice(graph.indptr[i], graph.indptr[i + 1])
        # indices of corresponding neighbors
        i_indices = graph.indices[s]
        # corresponding connecting weights
        i_data = graph_data[s]
        total += np.sum(i_data * ((vals[i] - vals[i_indices]) ** 2))

    numer = (N - 1) * total
    denom = 2 * W * ((vals - vals_bar) ** 2).sum()
    C = numer / denom
        
    return C
    
def generate_null_genes_kh_081520(adata, gene_list, method, random_width=5):
    """
        Generate null gene set
        adata: AnnData
        gene_list: original gene list, should be a list of gene names
        method: One of 'mean_equal', 'mean_inflate'
        
        return a list of null genes
    """
    temp_df = pd.DataFrame(index=adata.var_names)
    temp_df['mean'] = np.array(adata.X.mean(axis=0)).reshape([-1])
    temp_df['rank'] = rankdata(temp_df['mean'], method='ordinal') - 1
    temp_df = temp_df.sort_values('rank')
    
    assert (method in ['mean_equal', 'mean_inflate']), "method must be in [mean_equal, mean_inflate]"
    
    if method == 'mean_equal':
        random_range = np.concatenate([np.arange(-random_width, 0), np.arange(1, random_width + 1)])
        
    if method == 'mean_inflate':
        random_range = np.arange(1, random_width + 1)
    
    # ordered gene_list
    gene_list_rank = sorted(temp_df.loc[gene_list, 'rank'].values)
    gene_list_null = []
    
    for rank in gene_list_rank:
        choices = set(rank + random_range) - set(gene_list_rank) - set(gene_list_null)
        gene_list_null.append(np.random.choice(list(choices)))
    
    # in case there is replicate / intersect with the gene_list_overlap
    gene_list_null = list(set(gene_list_null) - set(gene_list_rank))
    gene_list_null = temp_df.index[gene_list_null]
    
    return gene_list_null


def generate_null_dist_kh_081520(
               adata, 
               gene_list, 
               flag_correct_background=False,
               flag_nullgene=False,
               random_seed=0,
               verbose=True):
    
    """Generate null distributions
    
    Args:
        data (AnnData): AnnData object
            adata.X should contain size-normalized log1p transformed count data
        gene_list (list): gene list 
        flag_correct_background (bool):
            If normalize for background mean and std. If True, normalize by 
            score = (score - mean)/std
        tissue (str): 'all' or one of the facs or droplet tissues
        

    Returns:
        A dict with different null distributions
    """
    dic_null_dist = dict()
    np.random.seed(random_seed)
    gene_list_overlap = list(set(adata.var_names) & set(gene_list))
    if verbose:
        print('# generate_null_dist: %d/%d gene_list genes also in adata'
              %(len(gene_list), len(gene_list_overlap)))
        print('# generate_null_dist: flag_correct_background=%s'
              %(flag_correct_background))
        
    # Compute TRS with simple average
    dic_null_dist['TRS'] = adata[:, gene_list_overlap].X.mean(axis=1).A1
    
    if flag_nullgene:
        temp_df = pd.DataFrame(index=adata.var_names)
        temp_df['mean'] = np.array(adata.X.mean(axis=0)).reshape([-1])
    
        # A random set
        ind_select = np.random.permutation(adata.shape[1])[:len(gene_list_overlap)]
        gene_list_null = list(adata.var_names[ind_select])
        dic_null_dist['nullgene_random'] = adata[:, gene_list_null].X.mean(axis=1).A1
        
        # Random set with matching mean expression
        gene_list_null_me = generate_null_genes(adata, gene_list_overlap, method='mean_equal')
        dic_null_dist['nullgene_mean_equal'] = adata[:, gene_list_null_me].X.mean(axis=1).A1
        
        if verbose:
            print('# generate_null_dist: %d trait genes with mean_exp=%0.3f'
                   %(len(gene_list_overlap), temp_df.loc[gene_list_overlap,'mean'].values.mean()))
            print('# generate_null_dist: %d null_me genes with mean_exp=%0.3f'
                   %(len(gene_list_null_me), temp_df.loc[gene_list_null_me,'mean'].values.mean()))
        
    
    # Cell background correction
    if flag_correct_background:
        v_mean,v_var = util.get_sparse_var(adata.X, axis=1)
        v_std = np.sqrt(v_var)
        dic_null_dist['TRS'] = (dic_null_dist['TRS'] - v_mean) / v_std * \
                                np.sqrt(len(gene_list_overlap))
        if flag_nullgene:
            dic_null_dist['nullgene_random'] = \
                (dic_null_dist['nullgene_random'] - v_mean) / v_std * np.sqrt(len(gene_list_null))
            
            dic_null_dist['nullgene_mean_equal'] = \
                (dic_null_dist['nullgene_mean_equal'] - v_mean) / v_std * np.sqrt(len(gene_list_null_me))
    
    return dic_null_dist