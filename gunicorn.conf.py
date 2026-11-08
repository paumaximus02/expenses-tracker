# Gunicorn's default 30s worker timeout kills an insight request while Grok
# is still writing the answer. The start command does not have to repeat this.
timeout = 150
