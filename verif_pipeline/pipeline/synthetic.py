"""Writers for local synthetic input datasets (fully offline)."""
from __future__ import annotations

import csv

import netCDF4
import numpy as np

from .io_forecast import MANIFEST_FIELDS
from .io_obs import OBS_FIELDS, STATION_FIELDS


def write_forecast_nc(path, cycle, member, leads_hours, lats, lons, apcp,
                      reset_interval_hours):
    """Write one forecast NetCDF file (accumulated precipitation)."""
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
        v = ds.createVariable("apcp", "f8", ("time", "lat", "lon"))
        v.units = "mm"
        v.long_name = "accumulated precipitation since accumulation period start"
        v[:] = apcp
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
