"""Deterministic operations. Raw data is never interpreted as executable code."""
import math

import numpy as np
import pandas as pd


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        value = value.item()
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if value is None or (isinstance(value, float) and not math.isfinite(value)) or value is pd.NaT or value is pd.NA:
        return None
    return value


def numeric(series):
    return pd.to_numeric(series.map(lambda value: None if isinstance(value, (bool, np.bool_)) else value),
                         errors="coerce").replace([np.inf, -np.inf], np.nan)


def stats(series):
    values = numeric(series).dropna()
    return clean({"count": len(values), "missing": len(series) - len(values),
                  "sum": float(values.sum()) if len(values) else None,
                  "mean": values.mean(), "median": values.median(),
                  "min": values.min(), "max": values.max(),
                  "stddev": values.std(ddof=0),
                  "p25": values.quantile(.25), "p75": values.quantile(.75),
                  "p95": values.quantile(.95)})


def dates(series):
    return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")


def field(frame, name):
    if not isinstance(name, str) or name not in frame.columns or name == "_geometry":
        raise ValueError("Choose an existing column from describe_source")
    return frame[name]


def filtered(frame, args):
    frame = frame.copy()
    for f in args.get("filters", []):
        col = field(frame, f["column"])
        value, op = f["value"], f["op"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            col = numeric(col)
        if op == "in":
            if not isinstance(value, list):
                raise ValueError("in requires a list")
            mask = col.isin(value)
        elif op == "contains":
            mask = col.astype("string").str.contains(str(value), regex=False, case=False, na=False)
        elif value is None and op in ("eq", "ne"):
            mask = col.isna() if op == "eq" else col.notna()
        else:
            fn = {"eq": "eq", "ne": "ne", "lt": "lt", "lte": "le", "gt": "gt", "gte": "ge"}[op]
            mask = getattr(col, fn)(value)
        frame = frame[mask.fillna(False)]
    if args.get("start") or args.get("end"):
        col = dates(field(frame, args.get("date_column")))
        if args.get("start"):
            frame = frame[col >= pd.to_datetime(args["start"], utc=True)]
        if args.get("end"):
            end = pd.to_datetime(args["end"], utc=True)
            if len(args["end"]) == 10:
                end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
            frame = frame[col.loc[frame.index] <= end]
    if args.get("polygon"):
        from shapely.geometry import Point, shape
        area = shape(args["polygon"])
        if not area.is_valid or area.is_empty:
            raise ValueError("Invalid area polygon")
        if "_geometry" in frame:
            mask = frame["_geometry"].map(lambda g: bool(g) and area.intersects(shape(g)))
        else:
            lat = numeric(field(frame, args.get("latitude_column")))
            lon = numeric(field(frame, args.get("longitude_column")))
            mask = pd.Series([math.isfinite(x) and math.isfinite(y) and area.covers(Point(x, y))
                              for x, y in zip(lon, lat)], index=frame.index)
        frame = frame[mask]
    return frame


def profile(frame):
    columns = []
    for name in frame.columns[:100]:
        if name == "_geometry":
            continue
        col = frame[name]
        valid = col.dropna()
        is_number = len(valid) > 0 and numeric(valid).notna().all()
        columns.append({"name": name, "type": "number" if is_number else "text",
                        "missing": int(col.isna().sum()), "unit": None})
    return {"rows_total": len(frame), "columns": columns,
            "sample": clean(frame.drop(columns=["_geometry"], errors="ignore").head(5).to_dict("records")),
            "sample_only": True}


def calculate(frame, args):
    total = len(frame)
    frame = filtered(frame, args)
    op = args["operation"]
    result = {"rows_total": total, "rows_used": len(frame), "complete": True,
              "filters": args.get("filters", []), "operation": op, "units": None}
    limit = min(200, args.get("limit", 200))
    if op == "rows":
        if args.get("order_by"):
            order = args["order_by"]
            col = field(frame, order)
            frame = frame.assign(_sort=numeric(col) if numeric(col).notna().sum() == col.notna().sum() else col)
            frame = frame.sort_values("_sort", ascending=not args.get("descending", False), kind="stable").drop(columns="_sort")
        columns = args.get("columns") or [c for c in frame.columns if c != "_geometry"][:20]
        for c in columns:
            field(frame, c)
        offset = args.get("offset", 0)
        result.update(rows=frame[columns].iloc[offset:offset + limit].to_dict("records"),
                      truncated=offset + limit < len(frame), offset=offset)
    elif op == "statistics":
        result["statistics"] = stats(field(frame, args.get("column")))
    elif op in ("group", "time_series"):
        metric = args.get("metric", "mean")
        values = numeric(field(frame, args.get("column"))) if metric != "count" else pd.Series(1, index=frame.index)
        if op == "time_series":
            dt = dates(field(frame, args.get("date_column")))
            keys = dt.dt.strftime({"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[args.get("period", "month")])
            result["invalid_dates"] = int(dt.isna().sum())
        else:
            keys = field(frame, args.get("group_by")).fillna("(missing)")
        groups = pd.DataFrame({"x": keys, "y": values}).groupby("x", dropna=True)["y"]
        aggregated = groups.std(ddof=0) if metric == "stddev" else groups.sum(min_count=1) if metric == "sum" else getattr(groups, metric)()
        counts = groups.count()
        if op == "group":
            aggregated = aggregated.sort_values(ascending=not args.get("descending", True), kind="stable")
        if len(aggregated) > 2000 and op == "time_series":
            raise ValueError("More than 2000 periods; choose a coarser period or shorter range")
        selected = aggregated if op == "time_series" else aggregated.head(limit)
        result.update(rows=[{"x": x, "y": v, "count": int(counts.loc[x])} for x, v in selected.items()],
                      truncated=len(selected) < len(aggregated), metric=metric)
    elif op == "trend":
        dt = dates(field(frame, args.get("date_column")))
        values = numeric(field(frame, args.get("column")))
        valid = dt.notna() & values.notna()
        dt, values = dt[valid], values[valid]
        if len(values) < 3 or (dt.max() - dt.min()).total_seconds() < 365.25 * 86400 / 2:
            raise ValueError("Annual trend requires at least three observations spanning six months")
        x = (dt - dt.min()).dt.total_seconds().to_numpy() / (365.25 * 86400)
        slope, intercept = np.polyfit(x, values.to_numpy(), 1)
        predicted = slope * x + intercept
        residual = float(((values - predicted) ** 2).sum())
        variance = float(((values - values.mean()) ** 2).sum())
        result["statistics"] = {"count": len(values), "slope_per_year": slope,
                                "r_squared": 1 - residual / variance if variance else None,
                                "start": dt.min(), "end": dt.max()}
    elif op == "correlation":
        a, b = numeric(field(frame, args.get("column"))), numeric(field(frame, args.get("column2")))
        pairs = pd.DataFrame({"x": a, "y": b}).dropna()
        if len(pairs) < 3 or pairs.x.nunique() < 2 or pairs.y.nunique() < 2:
            raise ValueError("Correlation requires three pairs and variation in both columns")
        result.update(statistics={"count": len(pairs), "pearson_r": pairs.x.corr(pairs.y)},
                      rows=pairs.head(limit).to_dict("records"), truncated=len(pairs) > limit,
                      warning="Correlation does not establish causation")
    elif op == "outliers":
        values = numeric(field(frame, args.get("column")))
        if values.notna().sum() < 4:
            raise ValueError("IQR outliers require at least four numeric observations")
        q1, q3 = values.quantile(.25), values.quantile(.75)
        low, high = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        matches = frame[(values < low) | (values > high)].drop(columns=["_geometry"], errors="ignore")
        result.update(statistics={"lower_fence": low, "upper_fence": high, "count": len(matches)},
                      rows=matches.iloc[:limit, :20].to_dict("records"), truncated=len(matches) > limit,
                      method="Tukey fences: Q1 - 1.5 IQR, Q3 + 1.5 IQR (linear quantiles)")
    else:
        raise ValueError("Unsupported operation")
    return clean(result)


def compare(left, right, operation):
    if not left.get("complete", True) or not right.get("complete", True):
        raise ValueError("Cannot compare incomplete results")
    if operation == "difference":
        if left.get("units") != right.get("units"):
            raise ValueError("Units do not match")
        if not left.get("units") and (not left.get("source_id") or left.get("source_id") != right.get("source_id")
                or left.get("parameters", {}).get("column") != right.get("parameters", {}).get("column")):
            raise ValueError("Units are unknown; only periods of the same source and column can be subtracted")
        a = left.get("statistics", {}).get("mean")
        b = right.get("statistics", {}).get("mean")
        if a is None or b is None:
            raise ValueError("Compute statistics for both periods first")
        return {"statistics": {"left_mean": a, "right_mean": b, "difference": b - a,
                               "percent_change": (b - a) / abs(a) * 100 if a else None},
                "units": left.get("units"), "complete": True}
    a, b = pd.DataFrame(left.get("rows", [])), pd.DataFrame(right.get("rows", []))
    if left.get("truncated") or right.get("truncated") or not {"x", "y"}.issubset(a) or not {"x", "y"}.issubset(b):
        raise ValueError("Compute complete time series for both sources first")
    if a.x.duplicated().any() or b.x.duplicated().any():
        raise ValueError("Aggregate duplicate dates before comparing series")
    paired = a[["x", "y"]].merge(b[["x", "y"]], on="x", suffixes=("_left", "_right"))
    result = calculate(paired, {"operation": "correlation", "column": "y_left", "column2": "y_right"})
    result["alignment"] = "Exact shared period keys; no interpolation"
    return result
