"""Usage data importers (step 2).

Each importer turns a source (Fox ESS cloud API, CSV upload) into a stream of
UsageInterval rows for one system. Historical weather for the same intervals is
fetched separately and joined on timestamp before training the model.
"""
