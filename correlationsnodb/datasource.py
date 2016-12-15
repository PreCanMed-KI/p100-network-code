
import pandas
import json
from datetime import datetime
import statsmodels.formula.api as smf
import statsmodels
import itertools
import numpy as np
from dataframeops import DataFrameOps

import logging
l_logger = logging.getLogger("p100.utils.correlations.datasource")

mb_tax = ['kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'species','otu']


class DataSourceFactory(object):

    """Factory object for generating and accessing datasources.
    """

    def __init__(self, ds_id_map, part_df, data_dir, round=None):
        self.database = None
        self._restrictions = {}
        self.round = round
        self.ds_map = pandas.read_pickle(ds_id_map)
        self.ds_id_map = ds_id_map
        self.part_df = part_df
        self.data_dir = data_dir

    def set_restrictions(self, min_obs=None, min_ent=None, min_fill=None,
                         normalize=None, rank=False):
        """Set default restrictions

        These restictions will propagate to all datasources generated by this
        factory.
        """
        self._restrictions['min_obs'] = min_obs
        self._restrictions['min_ent'] = min_ent
        self._restrictions['min_fill'] = min_fill
        self._restrictions['normalize'] = normalize
        self._restrictions['rank'] = rank


    def get_all_microbiome(self):
        ds = []
        for i, r in self.ds_map.iterrows():
            if r['type'] == 'MICRO':
                ds.append(self.get_by_ds_id(r['ds_id']))
        return ds

    @property
    def non_micro_ds(self):
        ds = []
        for i, r in self.ds_map.iterrows():
            if r['type'] != 'MICRO':
                ds.append(self.get_by_ds_id(r['ds_id']))
        return ds

    def get_all_comparisons(self):
        """Returns paired datasources for correlation.

        Base comparison is for inter- and intra-datasource correlations
        """
        non_micro = self.non_micro_ds
        comparisons = [(a, b) for a, b in
                       itertools.combinations(non_micro, 2)]
        comparisons += [(a, b)
                        for a, b in zip(non_micro, non_micro) if a.type not in ['GENOM']]
        mb = self.get_all_microbiome()
        comparisons += [(a, b) for a, b in zip(mb, mb)]
        comparisons = comparisons + [(a, b) for a, b in
                                     itertools.product(mb, non_micro)]
        return comparisons

    def get_by_ds_id(self, ds_id):
        ds = DataSource(self.ds_id_map, self.part_df, self.data_dir)
        ds.load(ds_id)
        if ds.type == 'CHEMS':
            c = ChemistriesDS(self.ds_id_map, self.part_df, self.data_dir)
            c.load(ds_id)
            return c
        if ds.type == 'MICRO':
            m = MicrobiomeDS(self.ds_id_map, self.part_df, self.data_dir)
            m.load(ds_id)
            return m
        if ds.type == 'MICLN':
            m = MicrobiomeLogDS(self.ds_id_map, self.part_df, self.data_dir)
            m.load(ds_id)
            return m
        if ds.type == 'METAB':
            m = MetabolomicsDS(self.ds_id_map, self.part_df, self.data_dir)
            m.load(ds_id)
            return m
        if ds.type == 'PROTE':
            p = ProteomicsDS(self.ds_id_map, self.part_df, self.data_dir)
            p.load(ds_id)
            return p
        if ds.type == 'GENOM':
            g = GenomicsDS(self.ds_id_map, self.part_df, self.data_dir)
            g.load(ds_id)
            return g
        if ds.type == 'COACH':
            f = CoachFeedbackDS(self.ds_id_map, self.part_df, self.data_dir)
            f.load(ds_id)
            return f
        raise Exception("Datasource not found ds_id[%i]" % ds_id)


class DataSource(object):

    """Base class for data wrappers for each of the various data types.

    The primary purpose of this class is to act as an interface and provide
    some common functionality for each of the correlating Datatypes.


    """

    def __init__(self, ds_id_map, part_df, data_dir, ds_type=None, aux=None, round=None):
        """Instantiates datasource.

        Args:
          database: (p100.database.Database) - DB to read and write to.
          ds_type: (str) The data type of this datasource. Should be a 5
              character upper case string like GENOM, PROTE, ...
          aux: (str) This is an auxialiary identifier, in case a data type has
              sub types like the taxonomic level of microbiome.  Note: the
              logic for this has to be handled in the subclass.
          round: (int) This restricts the select on the datasource to a single
              given round.
        """
        self.ds_type = ds_type
        self._aux = aux
        self._types = None
        self._annotations = None
        self.database = None
        self._restrictions = {}
        self._rest_func = []
        self._dsid = None
        self._round = round
        self.ds_map = pandas.read_pickle(ds_id_map)
        self.participants = pandas.read_pickle(part_df)
        self.data_dir = data_dir
        l_logger.debug("Initializing %s" %
                       (str(self)))

    @property
    def type(self):
        return self.ds_type

    @property
    def round(self):
        return self._round

    @property
    def aux(self):
        return self._aux


    @property
    def id(self):
        return self._dsid

    def uname_join(self, row):
        return '%s_%i' % (row['username'], row['round'])

    def delta_transform(self):
        if self.type in ['GENOM', 'COACH']:
            return self.GetDataFrame()
        else:
            df = self.GetDataFrame()
            df['round'] = [int(x[-1]) for x in df.index.tolist()]
            df['username'] = [x.split('_')[0] for x in df.index.tolist()]
            r1 = df[(df['round'] == 1)].set_index('username').drop('round', 1)
            r2 = df[(df['round'] == 2)].set_index('username').drop('round', 1)
            r3 = df[(df['round'] == 3)].set_index('username').drop('round', 1)
            r1_r2 = r2 - r1
            r2_r3 = r3 - r2
            r1_r2.index = ["%s_1" % x for x in r1_r2.index.tolist()]
            r2_r3.index = ["%s_2" % x for x in r2_r3.index.tolist()]
            joined = pandas.concat([r1_r2, r2_r3], axis=0)
            return self._apply_restrictions(joined)

    def agesex_adjust(self, df, sig_level=0.01):

        # Grab the participant data
        prt_data = self.participants

        for column in df.columns:

            # Merge the column with participant data
            sub = df.loc[:, [column]].merge(prt_data, left_index=True, right_on='username').dropna()
            sub.columns = ['value', 'username', 'gender', 'age', 'ancestry', 'genome_id']

            if (sub.shape[0]<20):
                continue

            model = smf.rlm(formula='value~age', data=sub, M=statsmodels.robust.norms.TrimmedMean())
            res = model.fit()

            if (res.pvalues['age']<sig_level):

                #print "corrected age", column, res.pvalues['age']
                temp = pandas.DataFrame(res.resid, columns=[column])

                ## Do partial regression
                #temp = pandas.DataFrame(smf.ols(formula='value~age', data=sub).fit().resid, columns=[column])

                # Set the index
                temp.index = sub['username']

                # Update the original dataframe
                df.update(temp)

            # Merge the column with participant data
            sub = df.loc[:, [column]].merge(prt_data, left_index=True, right_on='username').dropna()

            sub.columns = ['value', 'username', 'gender', 'age','ancestry', 'genome_id']

            model = smf.rlm(formula='value~C(gender)', data=sub, M=statsmodels.robust.norms.TrimmedMean())
            res = model.fit()

            if (res.pvalues['C(gender)[T.M]']<sig_level):

                temp = pandas.DataFrame(res.resid, columns=[column])

                ## Do partial regression
                #temp = pandas.DataFrame(smf.ols(formula='value~C(gender)', data=sub).fit().resid, columns=[column])

                # Set the index
                temp.index = sub['username']

                # Update the original dataframe
                df.update(temp)

        return df

    def GetDataFrame(self):
        pickle_file = '%s/%i.%s.%s.dataframe.pkl' % (self.data_dir, self.id, self.ds_type, self.aux)
        return pandas.read_pickle(pickle_file)

    @property
    def annotations(self):
        if self._annotations is None:
            pickle_file = '%s/%i.%s.%s.annotations.pkl' % (self.data_dir, self.id, self.ds_type, self.aux)
            self._annotations = pandas.read_pickle(pickle_file)
        return self._annotations

    def mean_transform_agesex_adjust(self, sig_level=0.01):
        l_logger.warning("Age sex transform")
        if self.type in ['GENOM', 'COACH']:
            df = self.GetDataFrame()
            df.loc(axis=1)['username'] = [x.split('_')[0] for x in df.index.tolist()]
            df = df.groupby(['username']).mean()
            return self._apply_restrictions(df)

        df = self.GetDataFrame()
        df.loc(axis=1)['username'] = [x.split('_')[0] for x in df.index.tolist()]
        df = df.groupby(['username']).mean()

        df = self.agesex_adjust(df, sig_level)
        return self._apply_restrictions(df)

    def delta_transform_agesex_adjust(self, sig_level=0.01):
        if self.type in ['GENOM', 'COACH']:
            return self.GetDataFrame()

        df = self.GetDataFrame()

       # Grab the participant data
        prt_data = self.participants
        # Build prt_data dataframe for multiple rounds
        prt_data1 = prt_data.copy()
        prt_data2 = prt_data.copy()
        prt_data3 = prt_data.copy()

        prt_data1['username'] = [x+'_1' for x in prt_data1['username'].tolist()]
        prt_data2['username'] = [x+'_2' for x in prt_data2['username'].tolist()]
        prt_data3['username'] = [x+'_3' for x in prt_data3['username'].tolist()]
        prt_data = pandas.concat([prt_data1, prt_data2, prt_data3], axis=0)

        for column in df.columns:

            # Merge the column with participant data
            sub = df.loc[:, [column]].merge(prt_data, left_index=True, right_on='username').dropna()
            sub.columns = ['value', 'username', 'gender', 'age', 'ancestry', 'genome_id']

            if (sub.shape[0]<20):
                continue

            model = smf.rlm(formula='value~age', data=sub, M=statsmodels.robust.norms.TrimmedMean())
            res = model.fit()

            if (res.pvalues['age']<sig_level):

                #print "corrected age", column, res.pvalues['age']
                temp = pandas.DataFrame(res.resid, columns=[column])

                ## Do partial regression
                #temp = pandas.DataFrame(smf.ols(formula='value~age', data=sub).fit().resid, columns=[column])

                # Set the index
                temp.index = sub['username']

                # Update the original dataframe
                df.update(temp)

            # Merge the column with participant data
            sub = df.loc[:, [column]].merge(prt_data, left_index=True, right_on='username').dropna()

            sub.columns = ['value', 'username', 'gender', 'age', 'ancestry', 'genome_id']

            model = smf.rlm(formula='value~C(gender)', data=sub, M=statsmodels.robust.norms.TrimmedMean())
            res = model.fit()

            if (res.pvalues['C(gender)[T.M]']<sig_level):

                temp = pandas.DataFrame(res.resid, columns=[column])

                ## Do partial regression
                #temp = pandas.DataFrame(smf.ols(formula='value~C(gender)', data=sub).fit().resid, columns=[column])

                # Set the index
                temp.index = sub['username']

                # Update the original dataframe
                df.update(temp)

        # Now split by round and calculate the delta values
        df['round'] = [int(x[-1]) for x in df.index.tolist()]
        df['username'] = [x.split('_')[0] for x in df.index.tolist()]
        r1 = df[(df['round'] == 1)].set_index('username').drop('round', 1)
        r2 = df[(df['round'] == 2)].set_index('username').drop('round', 1)
        r3 = df[(df['round'] == 3)].set_index('username').drop('round', 1)
        r1_r2 = r2 - r1
        r2_r3 = r3 - r2
        r1_r2.index = ["%s_1" % x for x in r1_r2.index.tolist()]
        r2_r3.index = ["%s_2" % x for x in r2_r3.index.tolist()]
        joined = pandas.concat([r1_r2, r2_r3], axis=0)
        return self._apply_restrictions(joined)

    def mean_transform(self):
        df = self.GetDataFrame()
        df.loc(axis=1)['username'] = [x.split('_')[0] for x in df.index.tolist()]
        df = df.groupby(['username']).mean()
        return self._apply_restrictions(df)

    def get_vector(self, id):
        return self.GetDataFrame()[id]

    def load(self, ds_id=None):
        """Given a datasource id, load the datasource object associated with
            that id.

        Args:
            ds_id (int): The id of the datasource to load.
        """
        if ds_id is not None:
            self._dsid = ds_id
        df = self.ds_map
        df.set_index('ds_id', inplace=True)
        self.ds_type = df.loc[self.id, 'type']
        self._aux = df.loc[self.id, 'aux']
        self._round = df.loc[self.id, 'round']
        r = df.loc[self.id, 'restrictions']
        if r and len(r) > 0:
            self._restrictions = json.loads(r)
        if "entropy" in self._restrictions:
            self._restrictions['min_ent'] = self._restrictions["entropy"]
            self._restrictions.pop('entropy')
        if "observations" in self._restrictions:
            self._restrictions['min_obs'] = self._restrictions["observations"]
            self._restrictions.pop('observations')
        self.restrict(**self._restrictions)

    def restrict(self, min_obs=None, min_ent=None, min_fill=None,
                 normalize=True, rank=False):
        """
        Restrict the dataframes
            min_obs(int) - minimum observations per variable (drop failing col)
            min_ent(float) - minimum entropy per variable (drop failing col)
            min_fill(float) - fill nan values with this percent of the minimum value in the column
                        (i.e .1 is 10% of the minimum value)
        """
        self._rest_func = []
        if rank:
            self._restrictions['rank'] = True
            self._rest_func.append(self._restrict_rank)
        if min_obs is not None:
            self._restrictions['observations'] = min_obs
            self._rest_func.append(self._restrict_observations)
        if min_ent is not None:
            self._restrictions['entropy'] = min_ent
            self._rest_func.append(self._restrict_entropy)
        if min_fill is not None:
            self._restrictions['fill'] = min_fill
            self._rest_func.append(self._restrict_fill)
        if normalize is not None:
            self._restrictions['normalize'] = normalize
            self._rest_func.append(self._restrict_normalize)

    def _restrict_rank(self, dataframe ):
        return dataframe.rank(axis=1, method='dense')

    def _restrict_observations(self, dataframe):
        return dataframe.dropna(thresh=self._restrictions['observations'], axis=1)

    def _restrict_entropy(self, dataframe):
        dfo = DataFrameOps()
        ent = dfo.get_entropy(dataframe)
        return dataframe.loc[:, ent > self._restrictions['entropy']]

    def _restrict_fill(self, dataframe):
        dfo = DataFrameOps()
        return dfo.min_fill(dataframe, adjust=self._restrictions['fill'])

    def _restrict_normalize(self, dataframe):
        l_logger.debug("Normalizing")
        try:
            centered = (dataframe - dataframe.mean())
        except:
            l_logger.exception(
                "Error centering dataframe dsid[%i]" % (self.id,))
            centered = dataframe
        try:
            return centered / dataframe.std()
        except:
            l_logger.exception(
                "Error normalizing dataframe dsid[%i]" % (self.id,))
            return centered

    def _apply_restrictions(self, dataframe):
        for f in self._rest_func:
            l_logger.debug( "Applying restricton %r" %f)
            dataframe = f(dataframe)
        # DEBUG - dataframe = dataframe[dataframe.columns[:10]]
        return dataframe

    def __str__(self):
        return "Datasource[%i]: %s, %s" % (self._dsid if self._dsid else -1, self.ds_type, self._aux)


class MicrobiomeDS(DataSource):

    def __init__(self, database, tax=None, round=None):
        self._aux = tax
        DataSource.__init__(self, database, 'MICRO', aux=tax, round=round)

    def annotate(self, t_id, format=None):
        if self._aux == 'diversity':
            tr_tax = ['diversity']
        else:
            tax = mb_tax 
            tr_tax = tax[:(tax.index(self._aux) + 1)]
        base = []
        if format is None:
            for t in tr_tax:
                ann = self.annotations.loc[t_id, "%s_desc" % t]
                base.append(ann)
            return '.'.join(base)



class MicrobiomeLogDS(MicrobiomeDS):

    def __init__(self, database, tax=None, round=None):
        self._aux = tax
        DataSource.__init__(self, database, 'MICLN', aux=tax, round=round)


    def _restrict_normalize(self, dataframe):
        l_logger.info("Normalizing log scores does not make sense to me")
        return dataframe


class ChemistriesDS(DataSource):

    def __init__(self, database, round=None):
        DataSource.__init__(self, database, 'CHEMS', None, round=round)

    def annotate(self, chem_id, format=None):
        if format is None:
            map_cols = ['vendor', 'name']
            chem = self.annotations.loc[chem_id][map_cols].tolist()
            return '.'.join(chem)

class MetabolomicsDS(DataSource):

    def __init__(self, database, round=None):
        DataSource.__init__(self, database, 'METAB', round=round)

    def annotate(self, met_id, format=None):
        if format is None:
            map_cols = ['super_pathway', 'sub_pathway', 'biochemical']
            meta = self.annotations.loc[met_id][map_cols].tolist()
            return '.'.join(meta)


class ProteomicsDS(DataSource):

    def __init__(self, database, round=None):
        DataSource.__init__(self, database, 'PROTE', None, round)

    def annotate(self, prot_id, format=None):
        if format is None:
            map_cols = ['category', 'abbreviation']
            meta = self.annotations.loc[prot_id][map_cols].tolist()
            return '.'.join(meta)


class GenomicsDS(DataSource):

    def __init__(self, database, type=None, round=None):
        DataSource.__init__(self, database, 'GENOM', type, round=round)
        self._aux = type
        # type is None of trait.  If trait, only combined traits will be
        # returned
        self._tset = None

    def annotate(self, study_trait_id, format=None):
        """
        This is kind cheesy
        """
        ann = self.annotations

        def clean_trait(tname):
            return tname.replace(' ', '-').lower()
        if format is None:
            return "%s.PMID%s.%s" % (
                clean_trait(ann.loc[study_trait_id, 'trait_name'])[:6],
                ann.loc[study_trait_id, 'pubmed'],
                clean_trait(ann.loc[study_trait_id, 'trait_name']))


class CoachFeedbackDS(DataSource):

    def __init__(self, database, type=None, round=None):
        DataSource.__init__(self, database, 'COACH', type, round=round)
        self._aux = 'coach'
        self._tset = None
