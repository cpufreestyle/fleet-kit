#!/usr/bin/env python3
"""Run a subset of the fleet chat test: python3 fleet_split.py 0 1 2 ..."""
import sys, os, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('fct', os.path.join(HERE, 'fleet_chat_test.py'))
fct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fct)
idxs = [int(x) for x in sys.argv[1:]] or list(range(len(fct.BRIDGES)))
fct.BRIDGES = [fct.BRIDGES[i] for i in idxs]
fct.main()
