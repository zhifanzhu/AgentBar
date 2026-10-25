"""What dscene needs from the app it runs inside (e.g. agentBar). Every hook has a default,
so dscene also works with no host at all: then it shows questions in its own popup and
deck cards only exist as swap_scene for MCP clients.

All hooks are called on the GTK main loop.
"""


class Host:
    def reserved_top(self):
        """Pixels at the top of the work area the host's own window covers; scenes go below."""
        return 0

    def quick_key(self):
        """The key the host maps to "the first option" of a tutorial step (e.g. "F4"), to show
        on its button; None if there's none. The host calls Stage.answer() when it's pressed."""
        return None

    def hide_chrome(self):
        """Get out of the way (a tutorial step is about to look at and mark the screen, or the
        user is about to drag a region). Return True if something was hidden."""
        return False

    def deck_preparing(self, count):
        """A deck of `count` scenes is being built (takes a second or two)."""

    def deck_changed(self, scenes, took=None):
        """A new deck is ready (scenes, built in `took` seconds), or the deck is gone ([])."""

    def deck_updated(self, index, scene):
        """Deck scene `index` changed (e.g. a dead link was swapped for a search)."""

    def showing(self, index, raise_host=True):
        """The deck scene now on screen, or None for the user's own windows."""

    def ask(self, question, options):
        """Show a question to the user; return True if the host shows it, False to let dscene
        show its own popup. Answers come back through Stage.answer()."""
        return False

    def ask_ended(self, answer, waited):
        """The question was answered (answer, after `waited` seconds) or withdrawn (None)."""
