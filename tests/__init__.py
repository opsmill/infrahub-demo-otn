"""Test suite for infrahub-demo-otn.

A real package rather than a bare directory. Two `conftest.py` files, one per
layer, are two modules called `conftest`, and mypy refuses to analyse both
unless something disambiguates them. A package does that; so did the
`namespace_packages` / `explicit_package_bases` pair this replaces.
"""
