from functools import partial
import operator

import streamz
from streamz import Stream
from dask.utils import M
import pandas as pd


class Streaming(object):
    _subtype = object

    def __init__(self, stream=None, example=None, columns=None):
        if columns is not None and example is None:
            example = pd.DataFrame({c: [] for c in columns})
        assert example is not None
        self.example = example
        assert isinstance(self.example, self._subtype)
        self.stream = stream or Stream()

    def map_partitions(self, func, *args, **kwargs):
        example = kwargs.pop('example', None)
        if example is None:
            example = func(self.example, *args, **kwargs)
        stream = self.stream.map(func, *args, **kwargs)

        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        elif isinstance(example, pd.Series):
            return StreamingSeries(stream, example)
        else:
            return Streaming(stream, example)

    def accumulate_partitions(self, func, *args, **kwargs):
        start = kwargs.pop('start', streamz.core.no_default)
        returns_state = kwargs.pop('returns_state', False)
        example = kwargs.pop('example', None)
        if example is None:
            example = func(start, self.example, *args, **kwargs)
        if returns_state:
            _, example = example
        stream = self.stream.accumulate(func, *args, start=start,
                returns_state=returns_state, **kwargs)

        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        elif isinstance(example, pd.Series):
            return StreamingSeries(stream, example)
        else:
            return Streaming(stream, example)

    def __repr__(self):
        example = self.example
        if hasattr(example, 'head'):
            example = example.head(2)
        return "%s - elements like:\n%r" % (type(self).__name__, example)

    def _repr_html_(self):
        example = self.example
        if hasattr(example, 'head'):
            example = example.head(2)
        try:
            body = example._repr_html_()
        except AttributeError:
            body = repr(example)

        return "<h5>%s - elements like<h5>\n%s" % (type(self).__name__, body)

    def __add__(self, other):
        return self.map_partitions(operator.add, other)

    def __mul__(self, other):
        return self.map_partitions(operator.mul, other)

    def __mod__(self, other):
        return self.map_partitions(operator.mod, other)

    def __truediv__(self, other):
        return self.map_partitions(operator.truediv, other)

    def __floordiv__(self, other):
        return self.map_partitions(operator.floordiv, other)

    def emit(self, x):
        self.verify(x)
        self.stream.emit(x)

    def verify(self, x):
        if not isinstance(x, self._subtype):
            raise TypeError("Expected type %s, got type %s" %
                            (self._subtype, type(x)))

    def rolling(self, window, min_periods=1):
        if not isinstance(window, int):
            window = pd.Timedelta(window)
            min_periods = 1
        stream = (self.stream
                      .scan(_roll, window=window,
                            min_periods=min_periods, returns_state=True)
                      .filter(lambda x: len(x) >= min_periods))
        return type(self)(stream=stream, example=self.example)


class StreamingFrame(Streaming):
    def sum(self):
        return self.accumulate_partitions(_accumulate_sum, start=0)

    def round(self, decimals=0):
        return self.map_partitions(M.round, decimals=decimals)


class StreamingDataFrame(StreamingFrame):
    _subtype = pd.DataFrame

    @property
    def columns(self):
        return self.example.columns

    def __getitem__(self, index):
        return self.map_partitions(operator.getitem, index)

    def __getattr__(self, key):
        if key in self.columns:
            return self.map_partitions(getattr, key)
        else:
            raise AttributeError("StreamingDataFrame has no attribute %r" % key)

    def __dir__(self):
        o = set(dir(type(self)))
        o.update(self.__dict__)
        o.update(c for c in self.columns if
                 (isinstance(c, pd.compat.string_types) and
                 pd.compat.isidentifier(c)))
        return list(o)

    def verify(self, x):
        super(StreamingDataFrame, self).verify(x)
        if list(x.columns) != list(self.example.columns):
            raise IndexError("Input expected to have columns %s, got %s" %
                             (self.example.columns, x.columns))

    def mean(self):
        start = pd.DataFrame({'sums': 0, 'counts': 0},
                             index=self.example.columns)
        return self.accumulate_partitions(_accumulate_mean, start=start,
                                          returns_state=True)

    def groupby(self, other):
        return StreamingSeriesGroupby(self, other)

    def assign(self, **kwargs):
        def concat(tup, columns=None):
            result = pd.concat(tup, axis=1)
            result.columns = columns
            return result
        columns, values = zip(*kwargs.items())
        stream = self.stream.zip(*[v.stream for v in values])
        stream = stream.map(concat, columns=list(self.columns) + list(columns))
        example = self.example.assign(**{c: v.example for c, v in kwargs.items()})
        return StreamingDataFrame(stream, example)

    def __setitem__(self, key, value):
        if isinstance(value, StreamingSeries):
            result = self.assign(**{key: value})
        elif isinstance(value, StreamingDataFrame):
            result = self.assign(**{k: value[c] for k, c in zip(key, value.columns)})
        else:
            example = self.example.copy()
            example[key] = value
            result = self.map_partitions(pd.DataFrame.assign, **{key: value})

        self.stream = result.stream
        self.example = result.example
        return self


class StreamingSeries(StreamingFrame):
    _subtype = pd.Series

    def mean(self):
        start = pd.Series({'sums': 0, 'counts': 0})
        return self.accumulate_partitions(_accumulate_mean, start=start,
                                          returns_state=True)


def _accumulate_mean(accumulator, new):
    accumulator = accumulator.copy()
    accumulator['sums'] += new.sum()
    accumulator['counts'] += new.count()
    result = accumulator['sums'] / accumulator['counts']
    return accumulator, result


def _accumulate_sum(accumulator, new):
    return accumulator + new.sum()


class StreamingSeriesGroupby(object):
    def __init__(self, root, grouper, index=None):
        self.root = root
        self.grouper = grouper
        self.index = index

    def __getitem__(self, index):
        return StreamingSeriesGroupby(self.root, self.grouper, index)

    def __getattr__(self, key):
        if key in self.root.columns:
            return self[key]
        else:
            raise AttributeError("StreamingSeriesGroupby has no attribute %r" % key)

    def sum(self):
        func = _accumulate_groupby_sum
        start = 0
        if isinstance(self.grouper, Streaming):
            func = partial(func, index=self.index)
            example = self.root.example.groupby(self.grouper.example)
            if self.index is not None:
                example = example[self.index]
            example = example.sum()
            stream = self.root.stream.zip(self.grouper.stream)
            stream = stream.accumulate(func, start=start)
        else:
            func = partial(func, grouper=self.grouper, index=self.index)
            example = self.root.example.groupby(self.grouper)
            if self.index is not None:
                example = example[self.index]
            example = example.sum()
            stream = self.root.stream.accumulate(func, start=start)
        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        else:
            return StreamingSeries(stream, example)

    def mean(self):
        # TODO, there is a lot of copy-paste with the code above
        # TODO, we should probably define groupby.aggregate
        func = _accumulate_groupby_mean
        start = (0, 0)
        if isinstance(self.grouper, Streaming):
            func = partial(func, index=self.index)
            example = self.root.example.groupby(self.grouper.example)
            if self.index is not None:
                example = example[self.index]
            example = example.mean()
            stream = self.root.stream.zip(self.grouper.stream)
            stream = stream.accumulate(func, start=start, returns_state=True)
        else:
            func = partial(func, grouper=self.grouper, index=self.index)
            example = self.root.example.groupby(self.grouper)
            if self.index is not None:
                example = example[self.index]
            example = example.mean()
            stream = self.root.stream.accumulate(func, start=start,
                                                 returns_state=True)
        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        else:
            return StreamingSeries(stream, example)


def _accumulate_groupby_sum(accumulator, new, grouper=None, index=None):
    if isinstance(new, tuple):  # zipped
        assert grouper is None
        new, grouper = new
    g = new.groupby(grouper)
    if index is not None:
        g = g[index]
    if isinstance(accumulator, int):
        return g.sum()
    else:
        return accumulator.add(g.sum(), fill_value=0)


def _accumulate_groupby_mean(accumulator, new, grouper=None, index=None):
    if isinstance(new, tuple):  # zipped
        assert grouper is None
        new, grouper = new
    g = new.groupby(grouper)
    if index is not None:
        g = g[index]

    (sums, counts) = accumulator
    if isinstance(sums, int):  # first time
        sums = g.sum()
        counts = g.count()
    else:
        sums = sums.add(g.sum(), fill_value=0)
        counts = counts.add(g.count(), fill_value=0)
    return (sums, counts), sums / counts


def _roll(accumulator, new, window, min_periods):
    accumulator = pd.concat([accumulator, new])
    if isinstance(window, int):
        accumulator = accumulator.iloc[-window:]
    elif isinstance(window, pd.Timedelta):
        accumulator = accumulator.loc[(accumulator.index.max() - window):]

    out = accumulator if len(accumulator) >= min_periods else []
    print(out)
    return accumulator, out
