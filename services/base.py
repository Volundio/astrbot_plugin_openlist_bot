class PluginService:
    """Base service that delegates shared helpers/state back to the plugin."""

    def __init__(self, plugin):
        self.plugin = plugin

    def __getattr__(self, name):
        return getattr(self.plugin, name)
