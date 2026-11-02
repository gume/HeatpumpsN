import pandas as pd


def run(series, threshold=0.5, window="1min"):
    s = series.dropna().sort_index()
    on = (s.astype(float) >= threshold).astype(float)
    vote = on.rolling(window, min_periods=1).mean()
    return (vote > 0.5).astype(int).rename(series.name)
