# Security Policy

## Operational safety

ROV SAR Web can send physical movement commands to a Titan T1 ROV. Treat every
deployment as safety-sensitive:

- keep `config.bat` and control tokens out of Git;
- use a strong `ROV_TOKEN` outside a closed test network;
- test STOP, deadman, and REAL/SIM transitions with the vehicle secured before
  an in-water trial;
- never expose the Django development server directly to the public internet.

## Reporting

Report a suspected vulnerability privately to the repository owner. Do not
publish control tokens, device addresses, packet captures containing sensitive
data, or reproduction steps that could endanger an operating vehicle.

