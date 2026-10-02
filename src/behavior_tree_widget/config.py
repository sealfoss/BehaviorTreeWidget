"""Tree execution options and the Configure dialog."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


@dataclass
class TreeConfig:
    """Options that control how a behavior tree is executed.

    Attributes:
        tick_interval_ms: Milliseconds between two ticks of the tree.
        repeat: If False (default) execution stops once the root node succeeds or
            fails. If True the tree keeps being ticked, restarting after completion.
        restore_blackboard: If True the blackboard values captured when execution
            starts are restored by Stop and Reset.
        default_memory: ``memory`` flag given to newly created Sequence/Selector
            nodes (see py_trees composites). With memory a composite resumes from
            its running child instead of re-ticking earlier children every tick.
    """

    tick_interval_ms: int = 100
    repeat: bool = False
    restore_blackboard: bool = False
    default_memory: bool = True

    MIN_TICK_MS = 1
    MAX_TICK_MS = 60_000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TreeConfig":
        """Build a config from saved data, ignoring unknown keys and invalid values."""
        config = cls()
        if not isinstance(data, dict):
            return config
        interval = data.get("tick_interval_ms")
        if isinstance(interval, int) and not isinstance(interval, bool):
            config.tick_interval_ms = max(cls.MIN_TICK_MS, min(cls.MAX_TICK_MS, interval))
        for name in ("repeat", "restore_blackboard", "default_memory"):
            if isinstance(data.get(name), bool):
                setattr(config, name, data[name])
        return config

    def copy(self) -> "TreeConfig":
        return replace(self)


class ConfigureDialog(QDialog):
    """Modal dialog editing a copy of a :class:`TreeConfig`."""

    RUN_ONCE_TEXT = "Run once (stop when the root completes)"
    REPEAT_TEXT = "Repeat (keep ticking after the root completes)"

    def __init__(self, config: TreeConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Configure Behavior Tree")
        self.setObjectName("ConfigureDialog")

        self.tick_interval = QSpinBox(self)
        self.tick_interval.setObjectName("TickInterval")
        self.tick_interval.setRange(TreeConfig.MIN_TICK_MS, TreeConfig.MAX_TICK_MS)
        self.tick_interval.setSuffix(" ms")
        self.tick_interval.setValue(config.tick_interval_ms)
        self.tick_interval.setToolTip("Time between two ticks of the behavior tree.")

        self.run_mode = QComboBox(self)
        self.run_mode.setObjectName("RunMode")
        self.run_mode.addItems([self.RUN_ONCE_TEXT, self.REPEAT_TEXT])
        self.run_mode.setCurrentIndex(1 if config.repeat else 0)

        self.restore_blackboard = QCheckBox("Restore blackboard values on Stop / Reset", self)
        self.restore_blackboard.setObjectName("RestoreBlackboard")
        self.restore_blackboard.setChecked(config.restore_blackboard)
        self.restore_blackboard.setToolTip(
            "Capture the blackboard when execution starts and put those values back on Stop or Reset."
        )

        self.default_memory = QCheckBox("New Sequence / Selector nodes use memory", self)
        self.default_memory.setObjectName("DefaultMemory")
        self.default_memory.setChecked(config.default_memory)
        self.default_memory.setToolTip(
            "With memory a composite resumes from its running child on the next tick instead of\n"
            "re-ticking the children before it. Each composite can be changed from its right-click menu."
        )

        form = QFormLayout()
        form.addRow("Tick interval:", self.tick_interval)
        form.addRow("Execution:", self.run_mode)
        form.addRow(self.restore_blackboard)
        form.addRow(self.default_memory)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        note = QLabel("Changes apply immediately, including to a running tree.", self)
        note.setEnabled(False)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def config(self) -> TreeConfig:
        """Return the options currently entered in the dialog."""
        return TreeConfig(
            tick_interval_ms=self.tick_interval.value(),
            repeat=self.run_mode.currentIndex() == 1,
            restore_blackboard=self.restore_blackboard.isChecked(),
            default_memory=self.default_memory.isChecked(),
        )
