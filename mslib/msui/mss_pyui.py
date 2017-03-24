#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    mslib.msui.mss_pyui
    ~~~~~~~~~~~~~~~~~~~

    Mission Support System Python/Qt4 User Interface
    Main window of the user interface application. Manages view and tool windows
    (the user can open multiple windows) and provides functionalty to open, save,
    and switch between flight tracks.

    This file is part of mss.

    :copyright: Copyright 2008-2014 Deutsches Zentrum fuer Luft- und Raumfahrt e.V.
    :copyright: Copyright 2011-2014 Marc Rautenhaus (mr)
    :copyright: Copyright 2016-2017 by the mss team, see AUTHORS.
    :license: APACHE-2.0, see LICENSE for details.

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""

import copy
import importlib
import logging
import os
import shutil
import sys
import types
import functools
import argparse

from mslib import __version__
from mslib.msui.mss_qt import ui_mainwindow as ui
from mslib.msui.mss_qt import ui_about_dialog as ui_ab
from mslib.msui import flighttrack as ft
from mslib.msui import tableview
from mslib.msui import topview
from mslib.msui import sideview
from mslib.msui import timeseriesview
from mslib.msui import trajectories_tool
from mslib.msui import loopview
from mslib.msui import constants
from mslib.utils import config_loader
from mslib.msui import MissionSupportSystemDefaultConfig as mss_default
from mslib.plugins.io.csv import load_from_csv, save_to_csv
from mslib.msui.icons import icons

# related third party imports
from mslib.msui.mss_qt import QtGui, QtCore, QtWidgets, _translate, _fromUtf8, USE_PYQT5

# Add config path to PYTHONPATH so plugins located there may be found
sys.path.append(constants.MSS_CONFIG_PATH)


class QActiveViewsListWidgetItem(QtWidgets.QListWidgetItem):
    """Subclass of QListWidgetItem, represents an open view in the list of
       open views. Keeps a reference to the view instance (i.e. the window) it
       represents in the list of open views.
    """

    # Class variable to assign a unique ID to each view.
    opened_views = 0

    def __init__(self, view_window, parent=None, viewsChanged=None,
                 type=QtWidgets.QListWidgetItem.UserType):
        """Add ID number to the title of the corresponing view window.
        """
        QActiveViewsListWidgetItem.opened_views += 1
        view_name = u"({:d}) {}".format(QActiveViewsListWidgetItem.opened_views, view_window.name)
        super(QActiveViewsListWidgetItem, self).__init__(view_name, parent, type)

        view_window.setWindowTitle(u"({:d}) {}".format(
            QActiveViewsListWidgetItem.opened_views, view_window.windowTitle()))
        view_window.setIdentifier(view_name)
        self.window = view_window
        self.parent = parent
        self.viewsChanged = viewsChanged

    def view_destroyed(self):
        """Slot that removes this QListWidgetItem from the parent (the
           QListWidget) if the corresponding view has been deleted.
        """
        if self.parent is not None:
            self.parent.takeItem(self.parent.row(self))
        if self.viewsChanged is not None:
            self.viewsChanged.emit()


class QFlightTrackListWidgetItem(QtWidgets.QListWidgetItem):
    """Subclass of QListWidgetItem, represents a flight track in the list of
       open flight tracks. Keeps a reference to the flight track instance
       (i.e. the instance of WaypointsTableModel).
    """

    def __init__(self, flighttrack_model, parent=None,
                 type=QtWidgets.QListWidgetItem.UserType):
        """Item class for the list widget that accomodates the open flight
           tracks.

        Arguments:
        flighttrack_model -- instance of a flight track model that is
                             associated with the item
        parent -- pointer to the QListWidgetItem that accomodates this item.
                  If not None, the itemChanged() signal of the parent is
                  connected to the nameChanged() slot of this class, reacting
                  to name changes of the item.
        """
        view_name = flighttrack_model.name
        super(QFlightTrackListWidgetItem, self).__init__(view_name, parent,
                                                         type)

        self.parent = parent
        self.flighttrack_model = flighttrack_model

        if parent is not None:
            parent.itemChanged.connect(self.nameChanged)

    def nameChanged(self, item):
        """Slot to change the name of a flight track.
        """
        item.flighttrack_model.setName(unicode(item.text()))


class MSS_AboutDialog(QtWidgets.QDialog, ui_ab.Ui_AboutMSUIDialog):
    """Dialog showing information about MSUI. Most of the displayed text is
       defined in the QtDesigner file.
    """

    def __init__(self, parent=None):
        """
        Arguments:
        parent -- Qt widget that is parent to this widget.
        """
        super(MSS_AboutDialog, self).__init__(parent)
        self.setupUi(self)
        self.lblVersion.setText(u"Version: {}".format(__version__))


class MSSMainWindow(QtWidgets.QMainWindow, ui.Ui_MSSMainWindow):
    """MSUI main window class. Provides user interface elements for managing
       flight tracks, views, and tools.
    """

    viewsChanged = QtCore.pyqtSignal(name="viewsChanged")

    def __init__(self, *args):
        super(MSSMainWindow, self).__init__(*args)
        self.setupUi(self)
        self.setWindowIcon(QtGui.QIcon('icons/64x64/mss-logo.png'))
        # This code is required in Windows 7 to use the icon set by setWindowIcon in taskbar
        # instead of the default Icon of python/pyhtonw
        try:
            import ctypes
            myappid = u"mss.mss_pyui.{}".format(__version__)  # arbitrary string
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except (ImportError, AttributeError):
            pass
        # Reference to the flight track that is currently displayed in the
        # views.
        self.active_flight_track = None
        self.lastSaveDir = os.getcwd()

        # Connect Qt SIGNALs:
        # ===================

        # File menu.
        self.actionNewFlightTrack.triggered.connect(functools.partial(self.createNewFlightTrack, None, None, False))
        self.actionOpenFlightTrack.triggered.connect(self.openFlightTrack)
        self.actionCloseSelectedFlightTrack.triggered.connect(self.closeFlightTrack)
        self.actionSaveActiveFlightTrack.triggered.connect(self.saveFlightTrack)
        self.actionSaveActiveFlightTrackAs.triggered.connect(self.saveFlightTrackAs)

        # Views menu.
        self.actionTopView.triggered.connect(self.createNewView)
        self.actionSideView.triggered.connect(self.createNewView)
        self.actionTableView.triggered.connect(self.createNewView)

        # Tools menu.
        self.actionTrajectoryToolLagranto.triggered.connect(self.createNewTool)
        self.actionTimeSeriesViewTrajectories.triggered.connect(self.createNewTool)
        self.actionLoopView.triggered.connect(self.createNewTool)

        # Help menu.
        self.actionOnlineHelp.triggered.connect(self.showOnlineHelp)
        self.actionAboutMSUI.triggered.connect(self.showAboutDlg)

        # Load Config
        self.actionLoad_Configuration.triggered.connect(self.openConfigFile)

        # Flight Tracks.
        self.listFlightTracks.itemChanged.connect(self.flightTrackNameChanged)
        self.btSelectFlightTrack.clicked.connect(self.setFlightTrackActive)

        # Views.
        self.listViews.itemActivated.connect(self.activateSubWindow)

        # Tools.
        self.listTools.itemActivated.connect(self.activateSubWindow)

        self.addImportFilter("CSV", "csv", load_from_csv)
        self.addExportFilter("CSV", "csv", save_to_csv)

        import_plugins = config_loader(dataset="import_plugins", default={})
        for name in import_plugins:
            extension, module, function = import_plugins[name]
            try:
                imported_module = importlib.import_module(module)
            except Exception, ex:
                QtWidgets.QMessageBox.critical(
                    self, self.tr("file io plugin error import plugins"),
                    self.tr(u"ERROR: Configuration\n\n{}\n\nthrows {} error:\n{}".format(
                        import_plugins, type(ex), ex)))
                continue
            try:
                self.addImportFilter(name, extension, getattr(imported_module, function))
            except AttributeError, ex:
                QtWidgets.QMessageBox.critical(
                    self, self.tr("file io plugin error import plugins"),
                    self.tr(u"ERROR: Configuration\n\n{}\n\nthrows {} error:\n{}".format(
                        import_plugins, type(ex), ex)))
                continue

        export_plugins = config_loader(dataset="export_plugins", default={})
        for name in export_plugins:
            extension, module, function = export_plugins[name]
            try:
                imported_module = importlib.import_module(module)
            except Exception, ex:
                QtWidgets.QMessageBox.critical(
                    self, self.tr("file io plugin error import plugins"),
                    self.tr(u"ERROR: Configuration\n\n{}\n\nthrows {} error:\n{}".format(
                        import_plugins, type(ex), ex)))
                continue
            try:
                    self.addExportFilter(name, extension, getattr(imported_module, function))
            except Exception, ex:
                QtWidgets.QMessageBox.critical(
                    self, self.tr("file io plugin error"),
                    self.tr(u"ERROR: Configuration for export {} plugins\n\n{}\n\nthrows error:\n{}".format(
                        export_plugins, type(ex), ex)))
                continue

        # self.actionLoopView.setVisible(config_loader(dataset="enable_loopview", default=False))

    def addImportFilter(self, name, extension, function):
        full_name = "actionImportFlightTrack" + name.replace(" ", "")

        if hasattr(self, full_name):
            raise ValueError(u"'{}' has already been set!".format(full_name))

        action = QtWidgets.QAction(self)
        action.setObjectName(_fromUtf8(full_name))
        action.setText(_translate("MSSMainWindow", name, None))
        self.menuImport_Flight_Track.addAction(action)

        def load_function_wrapper(self):
            filename = QtWidgets.QFileDialog.getOpenFileName(
                self, "Import Flight Track", "", name + " (*." + extension + ")")
            filename = filename[0] if USE_PYQT5 else unicode(filename)

            if filename:
                try:
                    ft_name, new_waypoints = function(filename)
                except (SyntaxError, IndexError, OSError, IOError), ex:
                    QtWidgets.QMessageBox.critical(
                        self, self.tr("file io plugin error"),
                        self.tr(u"ERROR: {} {}".format(type(ex), ex)))
                else:
                    if not ft_name:
                        ft_name = filename
                    waypoints_model = ft.WaypointsTableModel(name=ft_name, waypoints=new_waypoints)

                    listitem = QFlightTrackListWidgetItem(waypoints_model, self.listFlightTracks)
                    listitem.setFlags(QtCore.Qt.ItemIsEditable | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)

                    self.listFlightTracks.setCurrentItem(listitem)
                    self.setFlightTrackActive()

        setattr(self, full_name, types.MethodType(load_function_wrapper, self))
        action.triggered.connect(getattr(self, full_name))

    def addExportFilter(self, name, extension, function):
        full_name = "actionExportFlightTrack" + name.replace(" ", "")

        if hasattr(self, full_name):
            raise ValueError(u"'{}' has already been set!".format(full_name))

        action = QtWidgets.QAction(self)
        action.setObjectName(_fromUtf8(full_name))
        action.setText(_translate("MSSMainWindow", name, None))
        self.menuExport_Active_Flight_Track.addAction(action)

        def save_function_wrapper(self):
            filename = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export Flight Track", "", name + " (*." + extension + ")")
            filename = filename[0] if USE_PYQT5 else unicode(filename)

            if filename:
                try:
                    function(filename, self.active_flight_track.name, self.active_flight_track.waypoints)
                except (OSError, IOError), ex:
                    QtWidgets.QMessageBox.critical(
                        self, self.tr("file io plugin error"),
                        self.tr(u"ERROR: {} {}".format(type(ex), ex)))

        setattr(self, full_name, types.MethodType(save_function_wrapper, self))
        action.triggered.connect(getattr(self, full_name))

    def closeEvent(self, event):
        """Ask user if he/she wants to close the application. If yes, also
           close all views that are open.

        Overloads QtGui.QMainWindow.closeEvent(). This method is called if
        Qt receives a window close request for our application window.
        """
        ret = QtWidgets.QMessageBox.warning(
            self, self.tr("Mission Support System"),
            self.tr("Do you want to close the Mission Support System application?"),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)

        if ret == QtWidgets.QMessageBox.Yes:
            # Table View stick around after MainWindow closes - maybe some dangling reference?
            # This removes them for sure!
            self.listViews.clear()
            self.listTools.clear()
            self.listFlightTracks.clear()
            event.accept()
        else:
            event.ignore()

    def createNewView(self):
        """Method called when the user selects a new view to be opened. Creates
           a new instance of the view and adds a QActiveViewsListWidgetItem to
           the list of open views (self.listViews).
        """
        view_window = None
        if self.sender() == self.actionTopView:
            # Top view.
            view_window = topview.MSSTopViewWindow(model=self.active_flight_track)
        elif self.sender() == self.actionSideView:
            # Side view.
            view_window = sideview.MSSSideViewWindow(model=self.active_flight_track)
        elif self.sender() == self.actionTableView:
            # Table view.
            view_window = tableview.MSSTableViewWindow(model=self.active_flight_track)
        if view_window is not None:
            # Make sure view window will be deleted after being closed, not
            # just hidden (cf. Chapter 5 in PyQt4).
            view_window.setAttribute(QtCore.Qt.WA_DeleteOnClose)
            # Open as a non-modal window.
            view_window.show()
            # Add an entry referencing the new view to the list of views.
            listitem = QActiveViewsListWidgetItem(view_window, self.listViews, self.viewsChanged)
            view_window.viewCloses.connect(listitem.view_destroyed)
            self.listViews.setCurrentItem(listitem)
            self.viewsChanged.emit()

    def createNewTool(self):
        """Method called when the user selects a new tool to be opened. Creates
           a new instance of the tool and adds a QActiveViewsListWidgetItem to
           the list of open tools (self.listTools).
        """
        tool_window = None
        if self.sender() == self.actionTrajectoryToolLagranto:
            # Trajectory tool.
            tool_window = trajectories_tool.MSSTrajectoriesToolWindow(listviews=self.listViews,
                                                                      listtools=self.listTools,
                                                                      viewsChanged=self.viewsChanged)
        elif self.sender() == self.actionTimeSeriesViewTrajectories:
            # Time series view.
            tool_window = timeseriesview.MSSTimeSeriesViewWindow()
        elif self.sender() == self.actionLoopView:
            # Loop view.
            tool_window = loopview.MSSLoopWindow(
                config_loader(dataset="loop_configuration", default=mss_default.loop_configuration))

        if tool_window is not None:
            # Make sure view window will be deleted after being closed, not
            # just hidden (cf. Chapter 5 in PyQt4).
            tool_window.setAttribute(QtCore.Qt.WA_DeleteOnClose)
            # Open as a non-modal window.
            tool_window.show()
            # Add an entry referencing the new view to the list of views.
            listitem = QActiveViewsListWidgetItem(tool_window, self.listTools, self.viewsChanged)
            tool_window.viewCloses.connect(listitem.view_destroyed)
            self.viewsChanged.emit()

    def activateSubWindow(self, item):
        """When the user clicks on one of the open view or tool windows, this
           window is brought to the front. This function implements the slot to
           activate a window if the user selects it in the list of views or
           tools.
        """
        # Restore the activated view and bring it to the front.
        item.window.showNormal()
        item.window.raise_()
        item.window.activateWindow()

    new_flight_track_counter = 0

    def createNewFlightTrack(self, template=None, filename=None, activate=False):
        """Creates a new flight track model from a template. Adds a new entry to
           the list of flight tracks. Called when the user selects the 'new/open
           flight track' menu entries.

        Arguments:
        template -- copy the specified template to the new flight track (so that
                    it is not empty).
        filename -- if not None, load the flight track in the specified file.
        activate -- set the new flight track to be the active flight track.
        """
        if template is None:
            template = []
            waypoints = config_loader(dataset="new_flighttrack_template", default=mss_default.new_flighttrack_template)
            default_flightlevel = config_loader(dataset="new_flighttrack_flightlevel",
                                                default=mss_default.new_flighttrack_flightlevel)
            for wp in waypoints:
                template.append(ft.Waypoint(flightlevel=default_flightlevel, location=wp))
            if len(template) < 2:
                QtWidgets.QMessageBox.critical(
                    self, self.tr("flighttrack template"),
                    self.tr("ERROR:Flighttrack template in configuration is too short. "
                            "Please add at least two valid locations."))

        if filename:
            waypoints_model = ft.WaypointsTableModel(filename=filename)
        else:
            # Create a new flight track from the waypoints template.
            self.new_flight_track_counter += 1
            waypoints_model = ft.WaypointsTableModel(
                name="new flight track ({:d})".format(self.new_flight_track_counter))
            # Make a copy of the template. Otherwise all new flight tracks would
            # use the same data structure in memory.
            template_copy = copy.deepcopy(template)
            waypoints_model.insertRows(0, rows=len(template_copy),
                                       waypoints=template_copy)
        # Create a new list entry for the flight track. Make the item name
        # editable.
        listitem = QFlightTrackListWidgetItem(waypoints_model, self.listFlightTracks)
        listitem.setFlags(QtCore.Qt.ItemIsEditable | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)

        if activate:
            self.listFlightTracks.setCurrentItem(listitem)
            self.setFlightTrackActive()

    def openConfigFile(self):
        """
        Reads the config file

        Returns:

        """
        filename = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open Config file", "", "Supported files (*.json *.txt)")
        filename = filename[0] if USE_PYQT5 else unicode(filename)
        if filename:
            constants.CACHED_CONFIG_FILE = filename

    def openFlightTrack(self):
        """Slot for the 'Open Flight Track' menu entry. Opens a QFileDialog and
           passes the result to createNewFlightTrack().
        """
        filename = QtWidgets.QFileDialog.getOpenFileName(self,
                                                         "Open Flight Track", "",
                                                         "Flight track XML (*.ftml)")
        filename = filename[0] if USE_PYQT5 else unicode(filename)

        if filename:
            if filename.endswith('.ftml'):
                self.createNewFlightTrack(filename=filename, activate=True)
            else:
                QtWidgets.QMessageBox.warning(self, "Open flight track",
                                              u"No supported file extension recognized!\n{:}".format(filename))

    def closeFlightTrack(self):
        """Slot to close the currently selected flight track. Flight tracks can
           only be closed if at least one other flight track remains open. The
           currently active flight track cannot be closed.
        """
        if self.listFlightTracks.count() < 2:
            QtWidgets.QMessageBox.information(self, self.tr("Flight Track Management"),
                                              self.tr("At least one flight track has to be open."))
            return
        item = self.listFlightTracks.currentItem()
        if item.flighttrack_model == self.active_flight_track:
            QtWidgets.QMessageBox.information(self, self.tr("Flight Track Management"),
                                              self.tr("Cannot close currently active flight track."))
            return
        if item.flighttrack_model.modified:
            ret = QtWidgets.QMessageBox.warning(self, self.tr("Mission Support System"),
                                                self.tr("The flight track you are about to close has "
                                                        "been modified. Close anyway?"),
                                                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                                QtWidgets.QMessageBox.No)
            if ret == QtWidgets.QMessageBox.Yes:
                self.listFlightTracks.takeItem(self.listFlightTracks.currentRow())

    def saveFlightTrack(self):
        """Slot for the 'Save Active Flight Track As' menu entry.
        """
        filename = self.active_flight_track.getFilename()
        if filename and filename.endswith('.ftml'):
            sel = QtWidgets.QMessageBox.question(self, "Save flight track",
                                                 u"Saving flight track to '{:s}'. Continue?".format(filename),
                                                 QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if sel == QtWidgets.QMessageBox.Yes:
                self.active_flight_track.saveToFTML(filename)
        else:
            self.saveFlightTrackAs()

    def saveFlightTrackAs(self):
        """Slot for the 'Save Active Flight Track As' menu entry.
        """
        default_filename = os.path.join(self.lastSaveDir, self.active_flight_track.name + ".ftml")
        filename = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Flight Track", default_filename, "Flight track XML (*.ftml)")
        filename = filename[0] if USE_PYQT5 else unicode(filename)

        if filename:
            self.lastSaveDir = os.path.dirname(filename)
            if filename.endswith('.ftml'):
                self.active_flight_track.saveToFTML(filename)
            else:
                QtWidgets.QMessageBox.warning(self, "Save flight track",
                                              u"File extension is not '.ftml'!\n{:}".format(filename))

    def setFlightTrackActive(self):
        """Set the currently selected flight track to be the active one, i.e.
           the one that is displayed in the views (only one flight track can be
           displayed at a time).
        """
        item = self.listFlightTracks.currentItem()
        self.active_flight_track = item.flighttrack_model
        self.lblActiveFlightTrack.setText(item.flighttrack_model.name)
        for i in range(self.listViews.count()):
            view_item = self.listViews.item(i)
            view_item.window.setFlightTrackModel(self.active_flight_track)

    def flightTrackNameChanged(self, item):
        """Slot to react to a name change of the flight tracks in the list
           (i.e. when the user has edited a name). If the changed name
           belongs to the currently active flight track, change its name
           in the label that displays the active track.
        """
        if item.flighttrack_model == self.active_flight_track:
            self.lblActiveFlightTrack.setText(item.text())
        filename = item.flighttrack_model.filename \
            if item.flighttrack_model.filename else ""
        item.setToolTip(filename)

    def showOnlineHelp(self):
        """Open Documentation in a browser"""
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl("http://mss.readthedocs.io/en/stable"))

    def showAboutDlg(self):
        """Show the 'About MSUI' dialog to the user.
        """
        dlg = MSS_AboutDialog(parent=self)
        dlg.setModal(True)
        dlg.exec_()


def setup_logging(args):
    logger = logging.getLogger()
    # this is necessary as "someone" has already initialized logging, preventing basicConfig from doing stuff
    for ch in logger.handlers:
        logger.removeHandler(ch)

    debug_formatter = logging.Formatter("%(asctime)s (%(module)s.%(funcName)s:%(lineno)s): %(message)s")
    default_formatter = logging.Formatter("%(levelname)s: %(message)s")

    # Console handler (suppress DEBUG by default)
    ch = logging.StreamHandler()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(debug_formatter)
    else:
        logger.setLevel(logging.INFO)
        ch.setLevel(logging.INFO)
        ch.setFormatter(default_formatter)
    logger.addHandler(ch)
    # File handler (always on DEBUG level)
    if not args.nolog:
        logfile = os.path.join(constants.MSS_CONFIG_PATH, "mss_pyui.log")
        if args.logfile is not None:
            logfile = args.logfile
        try:
            fh = logging.FileHandler(logfile, "w")
        except (OSError, IOError), ex:
            logger.error("Could not open logfile '%s': %s %s", logfile, type(ex), ex)
        else:
            logger.setLevel(logging.DEBUG)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(debug_formatter)
            logger.addHandler(fh)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--version", help="show version", action="store_true", default=False)
    parser.add_argument("--debug", help="show debugging log messages on console", action="store_true", default=False)
    parser.add_argument("--logfile", help="specify logfile location", action="store", default=None)
    parser.add_argument("--nolog", help="do write debug log", action="store_true", default=False)
    parser.add_argument("-m", "--menue", help="adds mss to menue", action="store_true", default=False)

    args = parser.parse_args()

    if args.version:
        print "***********************************************************************"
        print "\n            Mission Support System (mss)\n"
        print "***********************************************************************"
        print "Documentation: http://mss.rtfd.io"
        print "Version:", __version__
        sys.exit()

    if args.menue:
        # Experimental feature to get mss into application menue
        if os.name == "posix":
            icon_size = '48x48'
            src_icon_path = icons(icon_size)
            icon_destination = constants.POSIX["icon_destination"].format(icon_size)
            dirname = os.path.dirname(icon_destination)
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            shutil.copyfile(src_icon_path, icon_destination)
            try:
                prefix = os.environ["CONDA_DEFAULT_ENV"]
            except KeyError:
                prefix = ""
            app_prefix = prefix
            if prefix:
                app_prefix = "-{}".format(prefix)
            desktop = constants.POSIX["desktop"]
            application_destination = constants.POSIX["application_destination"].format(app_prefix)
            desktop = desktop.format(prefix,
                                     os.path.join(sys.prefix, "bin", "mss"),
                                     icon_destination)
            with open(application_destination, 'w') as f:
                f.write(desktop)

            print "menue entry created"
            #ToDo implement uninstall by desktop-file-edit --delete-original
            sys.exit()


    setup_logging(args)

    logging.info("Launching user interface...")
    application = QtWidgets.QApplication(sys.argv)
    mainwindow = MSSMainWindow()
    mainwindow.createNewFlightTrack(activate=True)
    mainwindow.show()
    sys.exit(application.exec_())


if __name__ == "__main__":
    main()
