"""R11 custom-module support: the plug-in contract and its review gate.

**Off at launch (decision D5).** `loader.load_modules` returns nothing unless
`SCANNER_CUSTOM_MODULES_ENABLED=true`, so importing this package changes no
behaviour. It ships now because aeo-agent-service generates modules against this
contract, and a generator with no target contract writes code against an imagined
shape.
"""
