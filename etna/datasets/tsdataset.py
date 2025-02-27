import math
import warnings
from copy import copy
from copy import deepcopy
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from typing_extensions import Literal

from etna import SETTINGS
from etna.datasets.hierarchical_structure import HierarchicalStructure
from etna.datasets.utils import _TorchDataset
from etna.datasets.utils import get_level_dataframe
from etna.datasets.utils import inverse_transform_target_components
from etna.datasets.utils import match_target_quantiles
from etna.loggers import tslogger

if TYPE_CHECKING:
    from etna.transforms.base import Transform

if SETTINGS.torch_required:
    from torch.utils.data import Dataset

TTimestamp = Union[str, pd.Timestamp]


class TSDataset:
    """TSDataset is the main class to handle your time series data.
    It prepares the series for exploration analyzing, implements feature generation with Transforms
    and generation of future points.

    Notes
    -----
    TSDataset supports custom indexing and slicing method.
    It maybe done through these interface: ``TSDataset[timestamp, segment, column]``
    If at the start of the period dataset contains NaN those timestamps will be removed.

    During creation segment is casted to string type.

    Examples
    --------
    >>> from etna.datasets import generate_const_df
    >>> df = generate_const_df(periods=30, start_time="2021-06-01", n_segments=2, scale=1)
    >>> df_ts_format = TSDataset.to_dataset(df)
    >>> ts = TSDataset(df_ts_format, "D")
    >>> ts["2021-06-01":"2021-06-07", "segment_0", "target"]
    timestamp
    2021-06-01    1.0
    2021-06-02    1.0
    2021-06-03    1.0
    2021-06-04    1.0
    2021-06-05    1.0
    2021-06-06    1.0
    2021-06-07    1.0
    Freq: D, Name: (segment_0, target), dtype: float64

    >>> from etna.datasets import generate_ar_df
    >>> pd.options.display.float_format = '{:,.2f}'.format
    >>> df_to_forecast = generate_ar_df(100, start_time="2021-01-01", n_segments=1)
    >>> df_regressors = generate_ar_df(120, start_time="2021-01-01", n_segments=5)
    >>> df_regressors = df_regressors.pivot(index="timestamp", columns="segment").reset_index()
    >>> df_regressors.columns = ["timestamp"] + [f"regressor_{i}" for i in range(5)]
    >>> df_regressors["segment"] = "segment_0"
    >>> df_to_forecast = TSDataset.to_dataset(df_to_forecast)
    >>> df_regressors = TSDataset.to_dataset(df_regressors)
    >>> tsdataset = TSDataset(df=df_to_forecast, freq="D", df_exog=df_regressors, known_future="all")
    >>> tsdataset.df.head(5)
    segment      segment_0
    feature    regressor_0 regressor_1 regressor_2 regressor_3 regressor_4 target
    timestamp
    2021-01-01        1.62       -0.02       -0.50       -0.56        0.52   1.62
    2021-01-02        1.01       -0.80       -0.81        0.38       -0.60   1.01
    2021-01-03        0.48        0.47       -0.81       -1.56       -1.37   0.48
    2021-01-04       -0.59        2.44       -2.21       -1.21       -0.69  -0.59
    2021-01-05        0.28        0.58       -3.07       -1.45        0.77   0.28

    >>> from etna.datasets import generate_hierarchical_df
    >>> pd.options.display.width = 0
    >>> df = generate_hierarchical_df(periods=100, n_segments=[2, 4], start_time="2021-01-01",)
    >>> df, hierarchical_structure = TSDataset.to_hierarchical_dataset(df=df, level_columns=["level_0", "level_1"])
    >>> tsdataset = TSDataset(df=df, freq="D", hierarchical_structure=hierarchical_structure)
    >>> tsdataset.df.head(5)
    segment    l0s0_l1s3 l0s1_l1s0 l0s1_l1s1 l0s1_l1s2
    feature       target    target    target    target
    timestamp
    2021-01-01      2.07      1.62     -0.45     -0.40
    2021-01-02      0.59      1.01      0.78      0.42
    2021-01-03     -0.24      0.48      1.18     -0.14
    2021-01-04     -1.12     -0.59      1.77      1.82
    2021-01-05     -1.40      0.28      0.68      0.48
    """

    idx = pd.IndexSlice

    def __init__(
        self,
        df: pd.DataFrame,
        freq: str,
        df_exog: Optional[pd.DataFrame] = None,
        known_future: Union[Literal["all"], Sequence] = (),
        hierarchical_structure: Optional[HierarchicalStructure] = None,
    ):
        """Init TSDataset.

        Parameters
        ----------
        df:
            dataframe with timeseries
        freq:
            frequency of timestamp in df
        df_exog:
            dataframe with exogenous data;
        known_future:
            columns in ``df_exog[known_future]`` that are regressors,
            if "all" value is given, all columns are meant to be regressors
        hierarchical_structure:
            Structure of the levels in the hierarchy. If None, there is no hierarchical structure in the dataset.
        """
        self.raw_df = self._prepare_df(df)
        self.raw_df.index = pd.to_datetime(self.raw_df.index)
        self.freq = freq
        self.df_exog = None

        self.raw_df.index = pd.to_datetime(self.raw_df.index)

        try:
            inferred_freq = pd.infer_freq(self.raw_df.index)
        except ValueError:
            warnings.warn("TSDataset freq can't be inferred")
            inferred_freq = None

        if inferred_freq != self.freq:
            warnings.warn(
                f"You probably set wrong freq. Discovered freq in you data is {inferred_freq}, you set {self.freq}"
            )

        self.raw_df = self.raw_df.asfreq(self.freq)

        self.df = self.raw_df.copy(deep=True)

        self.known_future = self._check_known_future(known_future, df_exog)
        self._regressors = copy(self.known_future)

        self.hierarchical_structure = hierarchical_structure
        self.current_df_level: Optional[str] = self._get_dataframe_level(df=self.df)
        self.current_df_exog_level: Optional[str] = None

        if df_exog is not None:
            self.df_exog = df_exog.copy(deep=True)
            self.df_exog.index = pd.to_datetime(self.df_exog.index)
            self.current_df_exog_level = self._get_dataframe_level(df=self.df_exog)
            if self.current_df_level == self.current_df_exog_level:
                self.df = self._merge_exog(self.df)

        self._target_components_names: Tuple[str, ...] = tuple()

        self.df = self.df.sort_index(axis=1, level=("segment", "feature"))

    def _get_dataframe_level(self, df: pd.DataFrame) -> Optional[str]:
        """Return the level of the passed dataframe in hierarchical structure."""
        if self.hierarchical_structure is None:
            return None

        df_segments = df.columns.get_level_values("segment").unique()
        segment_levels = {self.hierarchical_structure.get_segment_level(segment=segment) for segment in df_segments}
        if len(segment_levels) != 1:
            raise ValueError("Segments in dataframe are from more than 1 hierarchical levels!")

        df_level = segment_levels.pop()
        level_segments = self.hierarchical_structure.get_level_segments(level_name=df_level)
        if len(df_segments) != len(level_segments):
            raise ValueError("Some segments of hierarchical level are missing in dataframe!")

        return df_level

    def transform(self, transforms: Sequence["Transform"]):
        """Apply given transform to the data."""
        self._check_endings(warning=True)
        for transform in transforms:
            tslogger.log(f"Transform {repr(transform)} is applied to dataset")
            transform.transform(self)

    def fit_transform(self, transforms: Sequence["Transform"]):
        """Fit and apply given transforms to the data."""
        self._check_endings(warning=True)
        for transform in transforms:
            tslogger.log(f"Transform {repr(transform)} is applied to dataset")
            transform.fit_transform(self)

    @staticmethod
    def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
        # cast segment to str type
        df_copy = df.copy(deep=True)
        columns_frame = df.columns.to_frame()
        columns_frame["segment"] = columns_frame["segment"].astype(str)
        df_copy.columns = pd.MultiIndex.from_frame(columns_frame)
        return df_copy

    def __repr__(self):
        return self.df.__repr__()

    def _repr_html_(self):
        return self.df._repr_html_()

    def __getitem__(self, item):
        if isinstance(item, slice) or isinstance(item, str):
            df = self.df.loc[self.idx[item]]
        elif len(item) == 2 and item[0] is Ellipsis:
            df = self.df.loc[self.idx[:], self.idx[:, item[1]]]
        elif len(item) == 2 and item[1] is Ellipsis:
            df = self.df.loc[self.idx[item[0]]]
        else:
            df = self.df.loc[self.idx[item[0]], self.idx[item[1], item[2]]]
        first_valid_idx = df.first_valid_index()
        df = df.loc[first_valid_idx:]
        return df

    def make_future(
        self, future_steps: int, transforms: Sequence["Transform"] = (), tail_steps: int = 0
    ) -> "TSDataset":
        """Return new TSDataset with features extended into the future.

        The result dataset doesn't contain quantiles and target components.

        Parameters
        ----------
        future_steps:
            number of steps to extend dataset into the future.
        transforms:
            sequence of transforms to be applied.
        tail_steps:
            number of steps to keep from the tail of the original dataset.

        Returns
        -------
        :
            dataset with features extended into the.

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df_regressors = pd.DataFrame({
        ...     "timestamp": list(pd.date_range("2021-06-01", periods=40))*2,
        ...     "regressor_1": np.arange(80), "regressor_2": np.arange(80) + 5,
        ...     "segment": ["segment_0"]*40 + ["segment_1"]*40
        ... })
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> df_regressors_ts_format = TSDataset.to_dataset(df_regressors)
        >>> ts = TSDataset(
        ...     df_ts_format, "D", df_exog=df_regressors_ts_format, known_future="all"
        ... )
        >>> ts.make_future(4)
        segment      segment_0                      segment_1
        feature    regressor_1 regressor_2 target regressor_1 regressor_2 target
        timestamp
        2021-07-01          30          35    NaN          70          75    NaN
        2021-07-02          31          36    NaN          71          76    NaN
        2021-07-03          32          37    NaN          72          77    NaN
        2021-07-04          33          38    NaN          73          78    NaN
        """
        self._check_endings(warning=True)
        max_date_in_dataset = self.df.index.max()
        future_dates = pd.date_range(
            start=max_date_in_dataset, periods=future_steps + 1, freq=self.freq, closed="right"
        )

        new_index = self.raw_df.index.append(future_dates)
        df = self.raw_df.reindex(new_index)
        df.index.name = "timestamp"

        if self.df_exog is not None and self.current_df_level == self.current_df_exog_level:
            df = self._merge_exog(df)

            # check if we have enough values in regressors
            if self.regressors:
                for segment in self.segments:
                    regressors_index = self.df_exog.loc[:, pd.IndexSlice[segment, self.regressors]].index
                    if not np.all(future_dates.isin(regressors_index)):
                        warnings.warn(
                            f"Some regressors don't have enough values in segment {segment}, "
                            f"NaN-s will be used for missing values"
                        )

        # remove components and quantiles
        # it should be done if we have quantiles and components in raw_df
        # TODO: fix this after making quantiles to work like components, with special methods
        if len(self.target_components_names) > 0:
            df = df.drop(columns=list(self.target_components_names), level="feature")
        if len(self.target_quantiles_names) > 0:
            df = df.drop(columns=list(self.target_quantiles_names), level="feature")

        # Here only df is required, other metadata is not necessary to build the dataset
        ts = TSDataset(df=df, freq=self.freq)
        for transform in transforms:
            tslogger.log(f"Transform {repr(transform)} is applied to dataset")
            transform.transform(ts)
        df = ts.to_pandas()

        future_dataset = df.tail(future_steps + tail_steps).copy(deep=True)

        future_dataset = future_dataset.sort_index(axis=1, level=(0, 1))
        future_ts = TSDataset(df=future_dataset, freq=self.freq, hierarchical_structure=self.hierarchical_structure)

        # can't put known_future into constructor, _check_known_future fails with df_exog=None
        future_ts.known_future = deepcopy(self.known_future)
        future_ts._regressors = deepcopy(self.regressors)
        if self.df_exog is not None:
            future_ts.df_exog = self.df_exog.copy(deep=True)
        return future_ts

    def tsdataset_idx_slice(self, start_idx: Optional[int] = None, end_idx: Optional[int] = None) -> "TSDataset":
        """Return new TSDataset with integer-location based indexing.

        Parameters
        ----------
        start_idx:
            starting index of the slice.
        end_idx:
            last index of the slice.

        Returns
        -------
        :
            TSDataset based on indexing slice.
        """
        df_slice = self.df.iloc[start_idx:end_idx].copy(deep=True)
        tsdataset_slice = TSDataset(df=df_slice, freq=self.freq)
        # can't put known_future into constructor, _check_known_future fails with df_exog=None
        tsdataset_slice.known_future = deepcopy(self.known_future)
        tsdataset_slice._regressors = deepcopy(self.regressors)
        if self.df_exog is not None:
            tsdataset_slice.df_exog = self.df_exog.copy(deep=True)
        tsdataset_slice._target_components_names = deepcopy(self._target_components_names)
        return tsdataset_slice

    @staticmethod
    def _check_known_future(
        known_future: Union[Literal["all"], Sequence], df_exog: Optional[pd.DataFrame]
    ) -> List[str]:
        """Check that ``known_future`` corresponds to ``df_exog`` and returns initial list of regressors."""
        if df_exog is None:
            exog_columns = set()
        else:
            exog_columns = set(df_exog.columns.get_level_values("feature"))

        if isinstance(known_future, str):
            if known_future == "all":
                return sorted(exog_columns)
            else:
                raise ValueError("The only possible literal is 'all'")
        else:
            known_future_unique = set(known_future)
            if not known_future_unique.issubset(exog_columns):
                raise ValueError(
                    f"Some features in known_future are not present in df_exog: "
                    f"{known_future_unique.difference(exog_columns)}"
                )
            else:
                return sorted(known_future_unique)

    @staticmethod
    def _check_regressors(df: pd.DataFrame, df_regressors: pd.DataFrame):
        """Check that regressors begin not later than in ``df`` and end later than in ``df``."""
        if df_regressors.shape[1] == 0:
            return
        # TODO: check performance
        df_segments = df.columns.get_level_values("segment")
        for segment in df_segments:
            target_min = df[segment]["target"].first_valid_index()
            target_min = pd.NaT if target_min is None else target_min
            target_max = df[segment]["target"].last_valid_index()
            target_max = pd.NaT if target_max is None else target_max

            exog_series_min = df_regressors[segment].first_valid_index()
            exog_series_min = pd.NaT if exog_series_min is None else exog_series_min
            exog_series_max = df_regressors[segment].last_valid_index()
            exog_series_max = pd.NaT if exog_series_max is None else exog_series_max
            if target_min < exog_series_min:
                raise ValueError(
                    f"All the regressor series should start not later than corresponding 'target'."
                    f"Series of segment {segment} have not enough history: "
                    f"{target_min} < {exog_series_min}."
                )
            if target_max >= exog_series_max:
                raise ValueError(
                    f"All the regressor series should finish later than corresponding 'target'."
                    f"Series of segment {segment} have not enough history: "
                    f"{target_max} >= {exog_series_max}."
                )

    def _merge_exog(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.df_exog is None:
            raise ValueError("Something went wrong, Trying to merge df_exog which is None!")
        df_regressors = self.df_exog.loc[:, pd.IndexSlice[:, self.known_future]]
        self._check_regressors(df=df, df_regressors=df_regressors)
        df = pd.concat((df, self.df_exog), axis=1).loc[df.index].sort_index(axis=1, level=(0, 1))
        return df

    def _check_endings(self, warning=False):
        """Check that all targets ends at the same timestamp."""
        max_index = self.df.index.max()
        if np.any(pd.isna(self.df.loc[max_index, pd.IndexSlice[:, "target"]])):
            if warning:
                warnings.warn(
                    "Segments contains NaNs in the last timestamps."
                    "Some of the transforms might work incorrectly or even fail."
                    "Make sure that you use the imputer before making the forecast."
                )
            else:
                raise ValueError("All segments should end at the same timestamp")

    def _inverse_transform_target_components(self, target_components_df: pd.DataFrame, target_df: pd.DataFrame):
        """Inverse transform target components in dataset with inverse transformed target."""
        self.drop_target_components()
        inverse_transformed_target_components_df = inverse_transform_target_components(
            target_components_df=target_components_df,
            target_df=target_df,
            inverse_transformed_target_df=self.to_pandas(features=["target"]),
        )
        self.add_target_components(target_components_df=inverse_transformed_target_components_df)

    def inverse_transform(self, transforms: Sequence["Transform"]):
        """Apply inverse transform method of transforms to the data.

        Applied in reversed order.
        """
        # TODO: return regressors after inverse_transform
        # Logic with target components is here for performance reasons.
        # This way we avoid doing the inverse transformation for components several times.
        target_components_present = len(self.target_components_names) > 0
        target_df, target_components_df = None, None
        if target_components_present:
            target_df = self.to_pandas(features=["target"])
            target_components_df = self.get_target_components()
            self.drop_target_components()

        try:
            for transform in reversed(transforms):
                tslogger.log(f"Inverse transform {repr(transform)} is applied to dataset")
                transform.inverse_transform(self)
        finally:
            if target_components_present:
                self._inverse_transform_target_components(
                    target_components_df=target_components_df, target_df=target_df
                )

    @property
    def segments(self) -> List[str]:
        """Get list of all segments in dataset.

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> ts = TSDataset(df_ts_format, "D")
        >>> ts.segments
        ['segment_0', 'segment_1']
        """
        return self.df.columns.get_level_values("segment").unique().tolist()

    @property
    def regressors(self) -> List[str]:
        """Get list of all regressors across all segments in dataset.

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> regressors_timestamp = pd.date_range(start="2021-06-01", periods=50)
        >>> df_regressors_1 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 1, "segment": "segment_0"}
        ... )
        >>> df_regressors_2 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 2, "segment": "segment_1"}
        ... )
        >>> df_exog = pd.concat([df_regressors_1, df_regressors_2], ignore_index=True)
        >>> df_exog_ts_format = TSDataset.to_dataset(df_exog)
        >>> ts = TSDataset(
        ...     df_ts_format, df_exog=df_exog_ts_format, freq="D", known_future="all"
        ... )
        >>> ts.regressors
        ['regressor_1']
        """
        return self._regressors

    @property
    def target_components_names(self) -> Tuple[str, ...]:
        """Get tuple with target components names. Components sum up to target. Return the empty tuple in case of components absence."""
        return self._target_components_names

    @property
    def target_quantiles_names(self) -> Tuple[str, ...]:
        """Get tuple with target quantiles names. Return the empty tuple in case of quantile absence."""
        return tuple(match_target_quantiles(features=set(self.columns.get_level_values("feature"))))

    def plot(
        self,
        n_segments: int = 10,
        column: str = "target",
        segments: Optional[Sequence[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        seed: int = 1,
        figsize: Tuple[int, int] = (10, 5),
    ):
        """Plot of random or chosen segments.

        Parameters
        ----------
        n_segments:
            number of random segments to plot
        column:
            feature to plot
        segments:
            segments to plot
        seed:
            seed for local random state
        start:
            start plot from this timestamp
        end:
            end plot at this timestamp
        figsize:
            size of the figure per subplot with one segment in inches
        """
        if segments is None:
            segments = self.segments
            k = min(n_segments, len(segments))
        else:
            k = len(segments)
        columns_num = min(2, k)
        rows_num = math.ceil(k / columns_num)
        start = self.df.index.min() if start is None else pd.Timestamp(start)
        end = self.df.index.max() if end is None else pd.Timestamp(end)

        figsize = (figsize[0] * columns_num, figsize[1] * rows_num)
        _, ax = plt.subplots(rows_num, columns_num, figsize=figsize, squeeze=False)
        ax = ax.ravel()
        rnd_state = np.random.RandomState(seed)
        for i, segment in enumerate(sorted(rnd_state.choice(segments, size=k, replace=False))):
            df_slice = self[start:end, segment, column]  # type: ignore
            ax[i].plot(df_slice.index, df_slice.values)
            ax[i].set_title(segment)
            ax[i].grid()

    @staticmethod
    def to_flatten(df: pd.DataFrame, features: Union[Literal["all"], Sequence[str]] = "all") -> pd.DataFrame:
        """Return pandas DataFrame with flatten index.

        The order of columns is (timestamp, segment, target,
        features in alphabetical order).

        Parameters
        ----------
        df:
            DataFrame in ETNA format.
        features:
            List of features to return.
            If "all", return all the features in the dataset.
            Always return columns with timestamp and segemnt.
        Returns
        -------
        pd.DataFrame:
            dataframe with TSDataset data

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df.head(5)
            timestamp    segment  target
        0  2021-06-01  segment_0    1.00
        1  2021-06-02  segment_0    1.00
        2  2021-06-03  segment_0    1.00
        3  2021-06-04  segment_0    1.00
        4  2021-06-05  segment_0    1.00
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> TSDataset.to_flatten(df_ts_format).head(5)
           timestamp    segment  target
        0 2021-06-01  segment_0    1.0
        1 2021-06-02  segment_0    1.0
        2 2021-06-03  segment_0    1.0
        3 2021-06-04  segment_0    1.0
        4 2021-06-05  segment_0    1.0
        """
        segments = df.columns.get_level_values("segment").unique()
        dtypes = df.dtypes
        category_columns = dtypes[dtypes == "category"].index.get_level_values(1).unique()
        if isinstance(features, str):
            if features != "all":
                raise ValueError("The only possible literal is 'all'")
        else:
            df = df.loc[:, pd.IndexSlice[segments, features]].copy()
        columns = df.columns.get_level_values("feature").unique()

        # flatten dataframe
        df_dict: Dict[str, Any] = {}
        df_dict["timestamp"] = np.tile(df.index, len(segments))
        df_dict["segment"] = np.repeat(segments, len(df.index))
        if "target" in columns:
            # set this value to lock position of key "target" in output dataframe columns
            # None is a placeholder, actual column value will be assigned in the following cycle
            df_dict["target"] = None
        for column in columns:
            df_cur = df.loc[:, pd.IndexSlice[:, column]]
            if column in category_columns:
                df_dict[column] = pd.api.types.union_categoricals([df_cur[col] for col in df_cur.columns])
            else:
                stacked = df_cur.values.T.ravel()
                # creating series is necessary for dtypes like "Int64", "boolean", otherwise they will be objects
                df_dict[column] = pd.Series(stacked, dtype=df_cur.dtypes[0])
        df_flat = pd.DataFrame(df_dict)

        return df_flat

    def to_pandas(self, flatten: bool = False, features: Union[Literal["all"], Sequence[str]] = "all") -> pd.DataFrame:
        """Return pandas DataFrame.

        Parameters
        ----------
        flatten:
            * If False, return pd.DataFrame with multiindex

            * If True, return with flatten index,
            its order of columns is (timestamp, segment, target,
            features in alphabetical order).
        features:
            List of features to return.
            If "all", return all the features in the dataset.
        Returns
        -------
        pd.DataFrame
            dataframe with TSDataset data

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df.head(5)
            timestamp    segment  target
        0  2021-06-01  segment_0    1.00
        1  2021-06-02  segment_0    1.00
        2  2021-06-03  segment_0    1.00
        3  2021-06-04  segment_0    1.00
        4  2021-06-05  segment_0    1.00
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> ts = TSDataset(df_ts_format, "D")
        >>> ts.to_pandas(True).head(5)
            timestamp    segment  target
        0  2021-06-01  segment_0    1.00
        1  2021-06-02  segment_0    1.00
        2  2021-06-03  segment_0    1.00
        3  2021-06-04  segment_0    1.00
        4  2021-06-05  segment_0    1.00
        >>> ts.to_pandas(False).head(5)
        segment    segment_0 segment_1
        feature       target    target
        timestamp
        2021-06-01      1.00      1.00
        2021-06-02      1.00      1.00
        2021-06-03      1.00      1.00
        2021-06-04      1.00      1.00
        2021-06-05      1.00      1.00
        """
        if not flatten:
            if isinstance(features, str):
                if features == "all":
                    return self.df.copy()
                raise ValueError("The only possible literal is 'all'")
            segments = self.columns.get_level_values("segment").unique().tolist()
            return self.df.loc[:, self.idx[segments, features]].copy()
        return self.to_flatten(self.df, features=features)

    @staticmethod
    def to_dataset(df: pd.DataFrame) -> pd.DataFrame:
        """Convert pandas dataframe to ETNA Dataset format.

        Columns "timestamp" and "segment" are required.

        Parameters
        ----------
        df:
            DataFrame with columns ["timestamp", "segment"]. Other columns considered features.

        Notes
        -----
        During conversion segment is casted to string type.

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df.head(5)
           timestamp    segment  target
        0 2021-06-01  segment_0    1.00
        1 2021-06-02  segment_0    1.00
        2 2021-06-03  segment_0    1.00
        3 2021-06-04  segment_0    1.00
        4 2021-06-05  segment_0    1.00
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> df_ts_format.head(5)
        segment    segment_0 segment_1
        feature       target    target
        timestamp
        2021-06-01      1.00      1.00
        2021-06-02      1.00      1.00
        2021-06-03      1.00      1.00
        2021-06-04      1.00      1.00
        2021-06-05      1.00      1.00

        >>> df_regressors = pd.DataFrame({
        ...     "timestamp": pd.date_range("2021-01-01", periods=10),
        ...     "regressor_1": np.arange(10), "regressor_2": np.arange(10) + 5,
        ...     "segment": ["segment_0"]*10
        ... })
        >>> TSDataset.to_dataset(df_regressors).head(5)
        segment      segment_0
        feature    regressor_1 regressor_2
        timestamp
        2021-01-01           0           5
        2021-01-02           1           6
        2021-01-03           2           7
        2021-01-04           3           8
        2021-01-05           4           9
        """
        df_copy = df.copy(deep=True)
        df_copy["timestamp"] = pd.to_datetime(df_copy["timestamp"])
        df_copy["segment"] = df_copy["segment"].astype(str)
        feature_columns = df_copy.columns.tolist()
        feature_columns.remove("timestamp")
        feature_columns.remove("segment")
        df_copy = df_copy.pivot(index="timestamp", columns="segment")
        df_copy = df_copy.reorder_levels([1, 0], axis=1)
        df_copy.columns.names = ["segment", "feature"]
        df_copy = df_copy.sort_index(axis=1, level=(0, 1))
        return df_copy

    @staticmethod
    def _hierarchical_structure_from_level_columns(
        df: pd.DataFrame, level_columns: List[str], sep: str
    ) -> HierarchicalStructure:
        """Create hierarchical structure from dataframe columns."""
        df_level_columns = df[level_columns].astype("string")

        prev_level_name = level_columns[0]
        for cur_level_name in level_columns[1:]:
            df_level_columns[cur_level_name] = (
                df_level_columns[prev_level_name] + sep + df_level_columns[cur_level_name]
            )
            prev_level_name = cur_level_name

        level_structure = {"total": list(df_level_columns[level_columns[0]].unique())}
        cur_level_name = level_columns[0]
        for next_level_name in level_columns[1:]:
            cur_level_to_next_level_edges = df_level_columns[[cur_level_name, next_level_name]].drop_duplicates()
            cur_level_to_next_level_adjacency_list = cur_level_to_next_level_edges.groupby(cur_level_name).agg(list)
            level_structure.update(cur_level_to_next_level_adjacency_list.to_records())
            cur_level_name = next_level_name

        hierarchical_structure = HierarchicalStructure(
            level_structure=level_structure, level_names=["total"] + level_columns
        )
        return hierarchical_structure

    @staticmethod
    def to_hierarchical_dataset(
        df: pd.DataFrame,
        level_columns: List[str],
        keep_level_columns: bool = False,
        sep: str = "_",
        return_hierarchy: bool = True,
    ) -> Tuple[pd.DataFrame, Optional[HierarchicalStructure]]:
        """Convert pandas dataframe from long hierarchical to ETNA Dataset format.

        Parameters
        ----------
        df:
            Dataframe in long hierarchical format with columns [timestamp, target] + [level_columns] + [other_columns]
        level_columns:
            Columns of dataframe defines the levels in the hierarchy in order
            from top to bottom i.e [level_name_1, level_name_2, ...]. Names of the columns will be used as
            names of the levels in hierarchy.
        keep_level_columns:
            If true, leave the level columns in the result dataframe.
            By default level columns are concatenated into "segment" column and dropped
        sep:
            String to concatenated the level names with
        return_hierarchy:
            If true, returns the hierarchical structure

        Returns
        -------
        :
            Dataframe in wide format and optionally hierarchical structure

        Raises
        ------
        ValueError
            If ``level_columns`` is empty
        """
        if len(level_columns) == 0:
            raise ValueError("Value of level_columns shouldn't be empty!")

        df_copy = df.copy(deep=True)
        df_copy["segment"] = df_copy[level_columns].astype("string").agg(sep.join, axis=1)
        if not keep_level_columns:
            df_copy.drop(columns=level_columns, inplace=True)
        df_copy = TSDataset.to_dataset(df_copy)

        hierarchical_structure = None
        if return_hierarchy:
            hierarchical_structure = TSDataset._hierarchical_structure_from_level_columns(
                df=df, level_columns=level_columns, sep=sep
            )

        return df_copy, hierarchical_structure

    def _find_all_borders(
        self,
        train_start: Optional[TTimestamp],
        train_end: Optional[TTimestamp],
        test_start: Optional[TTimestamp],
        test_end: Optional[TTimestamp],
        test_size: Optional[int],
    ) -> Tuple[TTimestamp, TTimestamp, TTimestamp, TTimestamp]:
        """Find borders for train_test_split if some values wasn't specified."""
        if test_end is not None and test_start is not None and test_size is not None:
            warnings.warn(
                "test_size, test_start and test_end cannot be applied at the same time. test_size will be ignored"
            )

        if test_end is None:
            if test_start is not None and test_size is not None:
                test_start_idx = self.df.index.get_loc(test_start)
                if test_start_idx + test_size > len(self.df.index):
                    raise ValueError(
                        f"test_size is {test_size}, but only {len(self.df.index) - test_start_idx} available with your test_start"
                    )
                test_end_defined = self.df.index[test_start_idx + test_size]
            elif test_size is not None and train_end is not None:
                test_start_idx = self.df.index.get_loc(train_end)
                test_start = self.df.index[test_start_idx + 1]
                test_end_defined = self.df.index[test_start_idx + test_size]
            else:
                test_end_defined = self.df.index.max()
        else:
            test_end_defined = test_end

        if train_start is None:
            train_start_defined = self.df.index.min()
        else:
            train_start_defined = train_start

        if train_end is None and test_start is None and test_size is None:
            raise ValueError("At least one of train_end, test_start or test_size should be defined")

        if test_size is None:
            if train_end is None:
                test_start_idx = self.df.index.get_loc(test_start)
                train_end_defined = self.df.index[test_start_idx - 1]
            else:
                train_end_defined = train_end

            if test_start is None:
                train_end_idx = self.df.index.get_loc(train_end)
                test_start_defined = self.df.index[train_end_idx + 1]
            else:
                test_start_defined = test_start
        else:
            if test_start is None:
                test_start_idx = self.df.index.get_loc(test_end_defined)
                test_start_defined = self.df.index[test_start_idx - test_size + 1]
            else:
                test_start_defined = test_start

            if train_end is None:
                test_start_idx = self.df.index.get_loc(test_start_defined)
                train_end_defined = self.df.index[test_start_idx - 1]
            else:
                train_end_defined = train_end

        if np.datetime64(test_start_defined) < np.datetime64(train_end_defined):
            raise ValueError("The beginning of the test goes before the end of the train")

        return train_start_defined, train_end_defined, test_start_defined, test_end_defined

    def train_test_split(
        self,
        train_start: Optional[TTimestamp] = None,
        train_end: Optional[TTimestamp] = None,
        test_start: Optional[TTimestamp] = None,
        test_end: Optional[TTimestamp] = None,
        test_size: Optional[int] = None,
    ) -> Tuple["TSDataset", "TSDataset"]:
        """Split given df with train-test timestamp indices or size of test set.

        In case of inconsistencies between ``test_size`` and (``test_start``, ``test_end``), ``test_size`` is ignored

        Parameters
        ----------
        train_start:
            start timestamp of new train dataset, if None first timestamp is used
        train_end:
            end timestamp of new train dataset, if None previous to ``test_start`` timestamp is used
        test_start:
            start timestamp of new test dataset, if None next to ``train_end`` timestamp is used
        test_end:
            end timestamp of new test dataset, if None last timestamp is used
        test_size:
            number of timestamps to use in test set

        Returns
        -------
        train, test:
            generated datasets

        Examples
        --------
        >>> from etna.datasets import generate_ar_df
        >>> pd.options.display.float_format = '{:,.2f}'.format
        >>> df = generate_ar_df(100, start_time="2021-01-01", n_segments=3)
        >>> df = TSDataset.to_dataset(df)
        >>> ts = TSDataset(df, "D")
        >>> train_ts, test_ts = ts.train_test_split(
        ...     train_start="2021-01-01", train_end="2021-02-01",
        ...     test_start="2021-02-02", test_end="2021-02-07"
        ... )
        >>> train_ts.df.tail(5)
        segment    segment_0 segment_1 segment_2
        feature       target    target    target
        timestamp
        2021-01-28     -2.06      2.03      1.51
        2021-01-29     -2.33      0.83      0.81
        2021-01-30     -1.80      1.69      0.61
        2021-01-31     -2.49      1.51      0.85
        2021-02-01     -2.89      0.91      1.06
        >>> test_ts.df.head(5)
        segment    segment_0 segment_1 segment_2
        feature       target    target    target
        timestamp
        2021-02-02     -3.57     -0.32      1.72
        2021-02-03     -4.42      0.23      3.51
        2021-02-04     -5.09      1.02      3.39
        2021-02-05     -5.10      0.40      2.15
        2021-02-06     -6.22      0.92      0.97
        """
        train_start_defined, train_end_defined, test_start_defined, test_end_defined = self._find_all_borders(
            train_start, train_end, test_start, test_end, test_size
        )

        if pd.Timestamp(test_end_defined) > self.df.index.max():
            warnings.warn(f"Max timestamp in df is {self.df.index.max()}.")
        if pd.Timestamp(train_start_defined) < self.df.index.min():
            warnings.warn(f"Min timestamp in df is {self.df.index.min()}.")

        train_df = self.df[train_start_defined:train_end_defined][self.raw_df.columns]  # type: ignore
        train_raw_df = self.raw_df[train_start_defined:train_end_defined]  # type: ignore
        train = TSDataset(
            df=train_df,
            df_exog=self.df_exog,
            freq=self.freq,
            known_future=self.known_future,
            hierarchical_structure=self.hierarchical_structure,
        )
        train.raw_df = train_raw_df
        train._regressors = deepcopy(self.regressors)
        train._target_components_names = deepcopy(self.target_components_names)

        test_df = self.df[test_start_defined:test_end_defined][self.raw_df.columns]  # type: ignore
        test_raw_df = self.raw_df[train_start_defined:test_end_defined]  # type: ignore
        test = TSDataset(
            df=test_df,
            df_exog=self.df_exog,
            freq=self.freq,
            known_future=self.known_future,
            hierarchical_structure=self.hierarchical_structure,
        )
        test.raw_df = test_raw_df
        test._regressors = deepcopy(self.regressors)
        test._target_components_names = deepcopy(self.target_components_names)
        return train, test

    def update_columns_from_pandas(self, df_update: pd.DataFrame):
        """Update the existing columns in the dataset with the new values from pandas dataframe.

        Before updating columns in df, columns of df_update will be cropped by the last timestamp in df.
        Columns in df_exog are not updated. If you wish to update the df_exog, create the new
        instance of TSDataset.

        Parameters
        ----------
        df_update:
            Dataframe with new values in wide ETNA format.
        """
        columns_to_update = sorted(set(df_update.columns.get_level_values("feature")))
        self.df.loc[:, self.idx[self.segments, columns_to_update]] = df_update.loc[
            : self.df.index.max(), self.idx[self.segments, columns_to_update]
        ]

    def add_columns_from_pandas(
        self, df_update: pd.DataFrame, update_exog: bool = False, regressors: Optional[List[str]] = None
    ):
        """Update the dataset with the new columns from pandas dataframe.

        Before updating columns in df, columns of df_update will be cropped by the last timestamp in df.

        Parameters
        ----------
        df_update:
            Dataframe with the new columns in wide ETNA format.
        update_exog:
             If True, update columns also in df_exog.
             If you wish to add new regressors in the dataset it is recommended to turn on this flag.
        regressors:
            List of regressors in the passed dataframe.
        """
        self.df = pd.concat((self.df, df_update[: self.df.index.max()]), axis=1).sort_index(axis=1)
        if update_exog:
            if self.df_exog is None:
                self.df_exog = df_update
            else:
                self.df_exog = pd.concat((self.df_exog, df_update), axis=1).sort_index(axis=1)
        if regressors is not None:
            self._regressors = list(set(self._regressors) | set(regressors))

    def drop_features(self, features: List[str], drop_from_exog: bool = False):
        """Drop columns with features from the dataset.

        Parameters
        ----------
        features:
            List of features to drop.
        drop_from_exog:
            * If False, drop features only from df. Features will appear again in df after make_future.
            * If True, drop features from df and df_exog. Features won't appear in df after make_future.

        Raises
        ------
        ValueError:
            If ``features`` list contains target components
        """
        features_contain_target_components = len(set(features).intersection(self.target_components_names)) > 0
        if features_contain_target_components:
            raise ValueError(
                "Target components can't be dropped from the dataset using this method! Use `drop_target_components` method!"
            )

        dfs = [("df", self.df)]
        if drop_from_exog:
            dfs.append(("df_exog", self.df_exog))

        for name, df in dfs:
            columns_in_df = df.columns.get_level_values("feature")
            columns_to_remove = list(set(columns_in_df) & set(features))
            unknown_columns = set(features) - set(columns_to_remove)
            if len(unknown_columns) > 0:
                warnings.warn(f"Features {unknown_columns} are not present in {name}!")
            if len(columns_to_remove) > 0:
                df.drop(columns=columns_to_remove, level="feature", inplace=True)
        self._regressors = list(set(self._regressors) - set(features))

    @property
    def index(self) -> pd.core.indexes.datetimes.DatetimeIndex:
        """Return TSDataset timestamp index.

        Returns
        -------
        pd.core.indexes.datetimes.DatetimeIndex
            timestamp index of TSDataset
        """
        return self.df.index

    def level_names(self) -> Optional[List[str]]:
        """Return names of the levels in the hierarchical structure."""
        if self.hierarchical_structure is None:
            return None
        return self.hierarchical_structure.level_names

    def has_hierarchy(self) -> bool:
        """Check whether dataset has hierarchical structure."""
        return self.hierarchical_structure is not None

    def get_level_dataset(self, target_level: str) -> "TSDataset":
        """Generate new TSDataset on target level.

        Parameters
        ----------
        target_level:
            target level name

        Returns
        -------
        TSDataset
            generated dataset
        """
        if self.hierarchical_structure is None or self.current_df_level is None:
            raise ValueError("Method could be applied only to instances with a hierarchy!")

        current_level_segments = self.hierarchical_structure.get_level_segments(level_name=self.current_df_level)
        target_level_segments = self.hierarchical_structure.get_level_segments(level_name=target_level)

        current_level_index = self.hierarchical_structure.get_level_depth(self.current_df_level)
        target_level_index = self.hierarchical_structure.get_level_depth(target_level)

        if target_level_index > current_level_index:
            raise ValueError("Target level should be higher in the hierarchy than the current level of dataframe!")

        target_names = self.target_quantiles_names + self.target_components_names + ("target",)

        if target_level_index < current_level_index:
            summing_matrix = self.hierarchical_structure.get_summing_matrix(
                target_level=target_level, source_level=self.current_df_level
            )

            target_level_df = get_level_dataframe(
                df=self.to_pandas(features=target_names),
                mapping_matrix=summing_matrix,
                source_level_segments=current_level_segments,
                target_level_segments=target_level_segments,
            )

        else:
            target_level_df = self.to_pandas(features=target_names)

        target_components_df = target_level_df.loc[:, pd.IndexSlice[:, self.target_components_names]]
        if len(self.target_components_names) > 0:  # for pandas >=1.1, <1.2
            target_level_df = target_level_df.drop(columns=list(self.target_components_names), level="feature")

        ts = TSDataset(
            df=target_level_df,
            freq=self.freq,
            df_exog=self.df_exog,
            known_future=self.known_future,
            hierarchical_structure=self.hierarchical_structure,
        )

        if len(self.target_components_names) > 0:
            ts.add_target_components(target_components_df=target_components_df)
        return ts

    def add_target_components(self, target_components_df: pd.DataFrame):
        """Add target components into dataset.

        Parameters
        ----------
        target_components_df:
            Dataframe in etna wide format with target components

        Raises
        ------
        ValueError:
            If dataset already contains target components
        ValueError:
            If target components names differs between segments
        ValueError:
            If components don't sum up to target
        """
        if len(self.target_components_names) > 0:
            raise ValueError("Dataset already contains target components!")

        components_names = sorted(target_components_df[self.segments[0]].columns.get_level_values("feature"))
        for segment in self.segments:
            components_names_segment = sorted(target_components_df[segment].columns.get_level_values("feature"))
            if components_names != components_names_segment:
                raise ValueError(
                    f"Set of target components differs between segments '{self.segments[0]}' and '{segment}'!"
                )

        components_sum = target_components_df.sum(axis=1, level="segment")
        if not np.allclose(components_sum.values, self[..., "target"].values):
            raise ValueError("Components don't sum up to target!")

        self._target_components_names = tuple(components_names)
        self.df = (
            pd.concat((self.df, target_components_df), axis=1)
            .loc[self.df.index]
            .sort_index(axis=1, level=("segment", "feature"))
        )

    def get_target_components(self) -> Optional[pd.DataFrame]:
        """Get DataFrame with target components.

        Returns
        -------
        :
            Dataframe with target components
        """
        if len(self.target_components_names) == 0:
            return None
        return self.to_pandas(features=self.target_components_names)

    def drop_target_components(self):
        """Drop target components from dataset."""
        if len(self.target_components_names) > 0:  # for pandas >=1.1, <1.2
            self.df.drop(columns=list(self.target_components_names), level="feature", inplace=True)
            self._target_components_names = ()

    @property
    def columns(self) -> pd.core.indexes.multi.MultiIndex:
        """Return columns of ``self.df``.

        Returns
        -------
        pd.core.indexes.multi.MultiIndex
            multiindex of dataframe with target and features.
        """
        return self.df.columns

    @property
    def loc(self) -> pd.core.indexing._LocIndexer:
        """Return self.df.loc method.

        Returns
        -------
        pd.core.indexing._LocIndexer
            dataframe with self.df.loc[...]
        """
        return self.df.loc

    def isnull(self) -> pd.DataFrame:
        """Return dataframe with flag that means if the correspondent object in ``self.df`` is null.

        Returns
        -------
        pd.Dataframe
            is_null dataframe
        """
        return self.df.isnull()

    def head(self, n_rows: int = 5) -> pd.DataFrame:
        """Return the first ``n_rows`` rows.

        Mimics pandas method.

        This function returns the first ``n_rows`` rows for the object based
        on position. It is useful for quickly testing if your object
        has the right type of data in it.

        For negative values of ``n_rows``, this function returns all rows except
        the last ``n_rows`` rows, equivalent to ``df[:-n_rows]``.

        Parameters
        ----------
        n_rows:
            number of rows to select.

        Returns
        -------
        pd.DataFrame
            the first ``n_rows`` rows or 5 by default.
        """
        return self.df.head(n_rows)

    def tail(self, n_rows: int = 5) -> pd.DataFrame:
        """Return the last ``n_rows`` rows.

        Mimics pandas method.

        This function returns last ``n_rows`` rows from the object based on
        position. It is useful for quickly verifying data, for example,
        after sorting or appending rows.

        For negative values of ``n_rows``, this function returns all rows except
        the first `n` rows, equivalent to ``df[n_rows:]``.

        Parameters
        ----------
        n_rows:
            number of rows to select.

        Returns
        -------
        pd.DataFrame
            the last ``n_rows`` rows or 5 by default.

        """
        return self.df.tail(n_rows)

    def _gather_common_data(self) -> Dict[str, Any]:
        """Gather information about dataset in general."""
        common_dict: Dict[str, Any] = {
            "num_segments": len(self.segments),
            "num_exogs": self.df.columns.get_level_values("feature").difference(["target"]).nunique(),
            "num_regressors": len(self.regressors),
            "num_known_future": len(self.known_future),
            "freq": self.freq,
        }

        return common_dict

    def _gather_segments_data(self, segments: Optional[Sequence[str]]) -> Dict[str, pd.Series]:
        """Gather information about each segment."""
        segments_index: Union[slice, Sequence[str]]
        if segments is None:
            segments_index = slice(None)
            segments = self.segments
        else:
            segments_index = segments
            segments = segments

        df = self.df.loc[:, (segments_index, "target")]

        num_timestamps = df.shape[0]
        not_na = ~np.isnan(df.values)
        min_idx = np.argmax(not_na, axis=0)
        max_idx = num_timestamps - np.argmax(not_na[::-1, :], axis=0) - 1

        segments_dict = {}
        segments_dict["start_timestamp"] = df.index[min_idx].to_series(index=segments)
        segments_dict["end_timestamp"] = df.index[max_idx].to_series(index=segments)
        segments_dict["length"] = pd.Series(max_idx - min_idx + 1, dtype="Int64", index=segments)
        segments_dict["num_missing"] = pd.Series(
            segments_dict["length"] - np.sum(not_na, axis=0), dtype="Int64", index=segments
        )

        # handle all-nans series
        all_nans_mask = np.all(~not_na, axis=0)
        segments_dict["start_timestamp"][all_nans_mask] = None
        segments_dict["end_timestamp"][all_nans_mask] = None
        segments_dict["length"][all_nans_mask] = None
        segments_dict["num_missing"][all_nans_mask] = None

        return segments_dict

    def describe(self, segments: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Overview of the dataset that returns a DataFrame.

        Method describes dataset in segment-wise fashion. Description columns:

        * start_timestamp: beginning of the segment, missing values in the beginning are ignored

        * end_timestamp: ending of the segment, missing values in the ending are ignored

        * length: length according to ``start_timestamp`` and ``end_timestamp``

        * num_missing: number of missing variables between ``start_timestamp`` and ``end_timestamp``

        * num_segments: total number of segments, common for all segments

        * num_exogs: number of exogenous features, common for all segments

        * num_regressors: number of exogenous factors, that are regressors, common for all segments

        * num_known_future: number of regressors, that are known since creation, common for all segments

        * freq: frequency of the series, common for all segments

        Parameters
        ----------
        segments:
            segments to show in overview, if None all segments are shown.

        Returns
        -------
        result_table: pd.DataFrame
            table with results of the overview

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> pd.options.display.expand_frame_repr = False
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> regressors_timestamp = pd.date_range(start="2021-06-01", periods=50)
        >>> df_regressors_1 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 1, "segment": "segment_0"}
        ... )
        >>> df_regressors_2 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 2, "segment": "segment_1"}
        ... )
        >>> df_exog = pd.concat([df_regressors_1, df_regressors_2], ignore_index=True)
        >>> df_exog_ts_format = TSDataset.to_dataset(df_exog)
        >>> ts = TSDataset(df_ts_format, df_exog=df_exog_ts_format, freq="D", known_future="all")
        >>> ts.describe()
                  start_timestamp end_timestamp  length  num_missing  num_segments  num_exogs  num_regressors  num_known_future freq
        segments
        segment_0      2021-06-01    2021-06-30      30            0             2          1               1                 1    D
        segment_1      2021-06-01    2021-06-30      30            0             2          1               1                 1    D
        """
        # gather common information
        common_dict = self._gather_common_data()

        # gather segment information
        segments_dict = self._gather_segments_data(segments)

        if segments is None:
            segments = self.segments

        # combine information
        segments_dict["num_segments"] = [common_dict["num_segments"]] * len(segments)
        segments_dict["num_exogs"] = [common_dict["num_exogs"]] * len(segments)
        segments_dict["num_regressors"] = [common_dict["num_regressors"]] * len(segments)
        segments_dict["num_known_future"] = [common_dict["num_known_future"]] * len(segments)
        segments_dict["freq"] = [common_dict["freq"]] * len(segments)

        result_df = pd.DataFrame(segments_dict, index=segments)
        columns_order = [
            "start_timestamp",
            "end_timestamp",
            "length",
            "num_missing",
            "num_segments",
            "num_exogs",
            "num_regressors",
            "num_known_future",
            "freq",
        ]
        result_df = result_df[columns_order]
        result_df.index.name = "segments"
        return result_df

    def info(self, segments: Optional[Sequence[str]] = None) -> None:
        """Overview of the dataset that prints the result.

        Method describes dataset in segment-wise fashion.

        Information about dataset in general:

        * num_segments: total number of segments

        * num_exogs: number of exogenous features

        * num_regressors: number of exogenous factors, that are regressors

        * num_known_future: number of regressors, that are known since creation

        * freq: frequency of the dataset

        Information about individual segments:

        * start_timestamp: beginning of the segment, missing values in the beginning are ignored

        * end_timestamp: ending of the segment, missing values in the ending are ignored

        * length: length according to ``start_timestamp`` and ``end_timestamp``

        * num_missing: number of missing variables between ``start_timestamp`` and ``end_timestamp``

        Parameters
        ----------
        segments:
            segments to show in overview, if None all segments are shown.

        Examples
        --------
        >>> from etna.datasets import generate_const_df
        >>> df = generate_const_df(
        ...    periods=30, start_time="2021-06-01",
        ...    n_segments=2, scale=1
        ... )
        >>> df_ts_format = TSDataset.to_dataset(df)
        >>> regressors_timestamp = pd.date_range(start="2021-06-01", periods=50)
        >>> df_regressors_1 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 1, "segment": "segment_0"}
        ... )
        >>> df_regressors_2 = pd.DataFrame(
        ...     {"timestamp": regressors_timestamp, "regressor_1": 2, "segment": "segment_1"}
        ... )
        >>> df_exog = pd.concat([df_regressors_1, df_regressors_2], ignore_index=True)
        >>> df_exog_ts_format = TSDataset.to_dataset(df_exog)
        >>> ts = TSDataset(df_ts_format, df_exog=df_exog_ts_format, freq="D", known_future="all")
        >>> ts.info()
        <class 'etna.datasets.TSDataset'>
        num_segments: 2
        num_exogs: 1
        num_regressors: 1
        num_known_future: 1
        freq: D
                  start_timestamp end_timestamp  length  num_missing
        segments
        segment_0      2021-06-01    2021-06-30      30            0
        segment_1      2021-06-01    2021-06-30      30            0
        """
        if segments is None:
            segments = self.segments
        lines = []

        # add header
        lines.append("<class 'etna.datasets.TSDataset'>")

        # add common information
        common_dict = self._gather_common_data()

        for key, value in common_dict.items():
            lines.append(f"{key}: {value}")

        # add segment information
        segments_dict = self._gather_segments_data(segments)
        segment_df = pd.DataFrame(segments_dict, index=segments)
        segment_df.index.name = "segments"

        with pd.option_context("display.width", None):
            lines += segment_df.to_string().split("\n")

        # print the results
        result_string = "\n".join(lines)
        print(result_string)

    def to_torch_dataset(
        self, make_samples: Callable[[pd.DataFrame], Union[Iterator[dict], Iterable[dict]]], dropna: bool = True
    ) -> "Dataset":
        """Convert the TSDataset to a :py:class:`torch.Dataset`.

        Parameters
        ----------
        make_samples:
            function that takes per segment DataFrame and returns iterabale of samples
        dropna:
            if ``True``, missing rows are dropped

        Returns
        -------
        :
            :py:class:`torch.Dataset` with with train or test samples to infer on
        """
        df = self.to_pandas(flatten=True)
        if dropna:
            df = df.dropna()  # TODO: Fix this

        ts_segments = [df_segment for _, df_segment in df.groupby("segment")]
        ts_samples = [samples for df_segment in ts_segments for samples in make_samples(df_segment)]

        return _TorchDataset(ts_samples=ts_samples)
