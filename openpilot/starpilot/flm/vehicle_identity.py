"""Shared parked/live vehicle binding when the diagnostic VIN is unavailable.

An unknown VIN is never replaced with a fabricated VIN. The fallback is local
to a registered device and its exact firmware-bearing CarParams. Moving that
device to another car requires resetting Auto Learn, including identical cars.
"""

import re

from opendbc.car.vin import VIN_UNKNOWN, is_valid_vin


def identity_keys(cp):
  vin = str(cp.carVin)
  return () if is_valid_vin(vin) and vin != VIN_UNKNOWN else ('DongleId',)


def persistent_unit(cp, raw):
  vin = str(cp.carVin)
  if is_valid_vin(vin) and vin != VIN_UNKNOWN:
    return vin
  device = raw.get('DongleId')
  if (type(device) is not bytes or re.fullmatch(rb'[0-9a-f]{16}', device) is None or
      not cp.carFw or not any(bytes(fw.fwVersion) for fw in cp.carFw)):
    raise ValueError('Auto Learn needs a VIN or a registered device with vehicle firmware')
  # The epoch also includes the full typed CP digest, controller, baseline and
  # saved settings. This is a device-local firmware binding, not a unique VIN.
  return ('device_firmware_vehicle_v1', device.hex())
