#!/bin/bash

set -eu

exec ./caddy run --config caddy_config.json
