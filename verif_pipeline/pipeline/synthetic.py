"""Writers for local synthetic input datasets (fully offline)."""
from __future__ import annotations

import csv

import netCDF4
import numpy as np

from .io_forecast import MANIFEST_FIELDS
from .io_obs import OBS_FIELDS, STATION_FIELDS


def write_forecast_nc(path, cycle, member, leads_hours, lats, lons, apcp,
                      reset_interval_hours, fill_value=None, missing_value=None,
                      packing=None):
    """Write one forecast NetCDF file (accumulated precipitation).

    ``apcp`` holds physical mm; NaN marks a missing value. When
    ``fill_value`` / ``missing_value`` are given, NaN cells are stored as
    that sentinel on disk (and the corresponding attribute is written), so
    readers exercise the declared sentinel handling. ``packing`` may be a
    dict ``{"scale_factor": s, "add_offset": o, "dtype": "i2"}`` to store
    the variable packed; sentinels are then given in packed units.
    """
    apcp = np.asarray(apcp, dtype="f8")
    nt, nlat, nlon = apcp.shape
    assert nt == len(leads_hours) and nlat == len(lats) and nlon == len(lons)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    try:
        ds.createDimension("time", nt)
        ds.createDimension("lat", nlat)
        ds.createDimension("lon", nlon)
        vt = ds.createVariable("lead_hours", "i4", ("time",))
        vt.units = "hours since cycle start"
        vt[:] = np.asarray(leads_hours, dtype="i4")
        vlat = ds.createVariable("lat", "f8", ("lat",))
        vlat.units = "degrees_north"
        vlat[:] = np.asarray(lats, dtype="f8")
        vlon = ds.createVariable("lon", "f8", ("lon",))
        vlon.units = "degrees_east"
        vlon[:] = np.asarray(lons, dtype="f8")
        kwargs = {"fill_value": fill_value} if fill_value is not None else {}
        if packing is not None:
            scale = float(packing["scale_factor"])
            offset = float(packing["add_offset"])
            dtype = packing.get("dtype", "i2")
            v = ds.createVariable("apcp", dtype, ("time", "lat", "lon"), **kwargs)
            v.scale_factor = scale
            v.add_offset = offset
            sentinel = fill_value if fill_value is not None else missing_value
            packed = np.where(np.isfinite(apcp),
                              np.round((apcp - offset) / scale), np.nan)
            if sentinel is not None:
                packed = np.where(np.isfinite(packed), packed, float(sentinel))
            v.set_auto_scale(False)  # store raw packed values as given
            v[:] = packed.astype(dtype)
        else:
            v = ds.createVariable("apcp", "f8", ("time", "lat", "lon"), **kwargs)
            sentinel = fill_value if fill_value is not None else missing_value
            data = apcp if sentinel is None else np.where(
                np.isfinite(apcp), apcp, float(sentinel))
            v[:] = data
        if missing_value is not None:
            v.missing_value = (np.array([missing_value]).astype(v.dtype)
                               if packing is not None else float(missing_value))
        v.units = "mm"
        v.long_name = "accumulated precipitation since accumulation period start"
        ds.cycle = str(cycle)
        ds.member = int(member)
        ds.reset_interval_hours = int(reset_interval_hours)
        ds.description = ("apcp is ACCUMULATED precipitation (mm); the counter "
                          "restarts every reset_interval_hours (0 = never)")
    finally:
        ds.close()


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_manifest(path, rows):
    _write_csv(path, MANIFEST_FIELDS, rows)


def write_obs(path, rows):
    _write_csv(path, OBS_FIELDS, rows)


def write_stations(path, rows):
    _write_csv(path, STATION_FIELDS, rows)
