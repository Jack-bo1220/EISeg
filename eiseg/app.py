import os
import os.path as osp
from functools import partial
import sys
import inspect

from qtpy import QtGui, QtCore, QtWidgets
from qtpy.QtWidgets import QMainWindow, QMessageBox, QTableWidgetItem
from qtpy.QtGui import QImage, QPixmap, QPolygonF, QPen
from qtpy.QtCore import Qt
import paddle
import cv2
import numpy as np
from PIL import Image
import matplotlib.pyplot as plt

import models
from controller import InteractiveController
from ui import Ui_EISeg, Ui_Help, PolygonAnnotation
from eiseg import pjpath, __APPNAME__
import util
from util.colormap import ColorMask
from util.label import Labeler
from util import MODELS

# DEBUG:
np.set_printoptions(threshold=sys.maxsize)


class APP_EISeg(QMainWindow, Ui_EISeg):
    IDILE, ANNING, EDITING = 0, 1, 2
    # IDILE：打开软件到模型和权重加载之前
    # ANNING：有未完成的交互式标注
    # EDITING：交互式标注完成，修改多边形

    def __init__(self, parent=None):
        super(APP_EISeg, self).__init__(parent)

        # 初始化界面
        self.setupUi(self)

        # app变量
        self.status = self.IDILE
        self.controller = None
        self.image = None  # 可能先加载图片后加载模型，只用于暂存图片
        # 默认显示为HRNet18s，默认的类别应该统一，否则直接加载报错
        self.modelClass = MODELS[0]
        self.outputDir = None  # 标签保存路径
        self.labelPaths = []  # 保存所有从outputdir发现的标签文件路径
        self.filePaths = []  # 文件夹下所有待标注图片路径
        self.currIdx = 0  # 文件夹标注当前图片下标
        self.currentPath = None
        self.isDirty = False
        self.labelList = Labeler()
        self.settings = QtCore.QSettings(
            osp.join(pjpath, "config/setting.ini"), QtCore.QSettings.IniFormat
        )
        self.config = util.parse_configs(osp.join(pjpath, "config/config.yaml"))
        self.recentFiles = self.settings.value("recent_files", [])
        self.recentModels = self.settings.value("recent_models", [])
        self.maskColormap = ColorMask(osp.join(pjpath, "config/colormap.txt"))

        # 初始化action
        self.initActions()

        # 更新模型使用记录
        self.updateModelsMenu()

        # 帮助界面
        self.help_dialog = QtWidgets.QDialog()
        help_ui = Ui_Help()
        help_ui.setupUi(self.help_dialog)

        ## 画布部分
        self.scene.clickRequest.connect(self.canvasClick)
        self.img_item = QtWidgets.QGraphicsPixmapItem()
        self.scene.addItem(self.img_item)

        ## 按钮点击
        self.btnSave.clicked.connect(self.saveLabel)  # 保存
        self.listFiles.itemDoubleClicked.connect(self.listClicked)  # 标签列表点击
        self.comboModelSelect.currentIndexChanged.connect(self.changeModel)  # 模型选择
        self.btnAddClass.clicked.connect(self.addLabel)
        self.btnParamsSelect.clicked.connect(self.changeParam)  # 模型参数选择

        ## 滑动
        self.sldOpacity.valueChanged.connect(self.maskOpacityChanged)
        self.sldClickRadius.valueChanged.connect(self.clickRadiusChanged)
        self.sldThresh.valueChanged.connect(self.threshChanged)

        ## 标签列表点击
        self.labelListTable.cellDoubleClicked.connect(self.labelListDoubleClick)
        self.labelListTable.cellClicked.connect(self.labelListClicked)
        self.labelListTable.cellChanged.connect(self.labelListItemChanged)
        labelListFile = self.settings.value("label_list_file")
        self.labelList.readLabel(labelListFile)
        self.refreshLabelList()

    def updateFileMenu(self):
        menu = self.actions.recent_files
        menu.clear()
        files = [f for f in self.recentFiles if osp.exists(f)]
        if self.currentPath in files:
            files.remove(self.currentPath)
        for i, f in enumerate(files):
            if osp.exists(f):
                icon = util.newIcon("File")
                action = QtWidgets.QAction(
                    icon, "&【%d】 %s" % (i + 1, QtCore.QFileInfo(f).fileName()), self
                )
                action.triggered.connect(partial(self.loadImage, f, True))
                menu.addAction(action)

    def updateModelsMenu(self):
        menu = self.actions.recent_params
        menu.clear()
        self.recentModels = [
            m for m in self.recentModels if osp.exists(m["param_path"])
        ]
        for idx, m in enumerate(self.recentModels):
            icon = util.newIcon("Model")
            action = QtWidgets.QAction(
                icon,
                f"&【{m['model_name']}】 {osp.basename(m['param_path'])}",
                self,
            )
            action.triggered.connect(
                partial(self.loadModelParam, m["param_path"], m["model_name"])
            )
            menu.addAction(action)
        self.settings.setValue("recent_params", self.recentModels)

    def delActivePolygon(self):
        for idx, polygon in enumerate(self.scene.polygon_items):
            if polygon.hasFocus():
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Warning)
                msg.setWindowTitle("确认删除？")
                msg.setText("确认要删除当前选中多边形标注？")
                msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
                res = msg.exec_()
                if res == QMessageBox.Yes:
                    polygon.remove()
                    del self.scene.polygon_items[idx]

    def delActivePoint(self):
        print("delActivePoint")
        for polygon in self.scene.polygon_items:
            polygon.removeFocusPoint()

    def initActions(self):
        def menu(title, actions=None):
            menu = self.menuBar().addMenu(title)
            if actions:
                util.addActions(menu, actions)
            return menu

        action = partial(util.newAction, self)
        shortcuts = self.config["shortcut"]
        del_active_point = action(
            self.tr("&删除点"),
            self.delActivePoint,
            shortcuts["del_active_point"],
            "Clear",
            self.tr("删除当前选中的点"),
        )
        del_active_polygon = action(
            self.tr("&删除多边形"),
            self.delActivePolygon,
            shortcuts["del_active_polygon"],
            "Clear",
            self.tr("删除当前选中的多边形"),
        )
        turn_prev = action(
            self.tr("&上一张"),
            partial(self.turnImg, -1),
            shortcuts["turn_prev"],
            "Prev",
            self.tr("翻到上一张图片"),
        )
        turn_next = action(
            self.tr("&下一张"),
            partial(self.turnImg, 1),
            shortcuts["turn_next"],
            "Next",
            self.tr("翻到下一张图片"),
        )
        open_image = action(
            self.tr("&打开图像"),
            self.openImage,
            shortcuts["open_image"],
            "OpenImage",
            self.tr("打开一张图像进行标注"),
        )
        open_folder = action(
            self.tr("&打开文件夹"),
            self.openFolder,
            shortcuts["open_folder"],
            "OpenFolder",
            self.tr("打开一个文件夹下所有的图像进行标注"),
        )
        open_recent = action(
            self.tr("&最近标注"),
            self.toBeImplemented,
            "",
            # TODO: 搞个图
            "",
            self.tr("打开一个文件夹下所有的图像进行标注"),
        )
        # model_loader = action(
        #     self.tr("&选择模型参数"),
        #     self.loadModel,
        #     shortcuts["load_model"],
        #     "Model",
        #     self.tr("加载一个模型参数"),
        # )
        change_output_dir = action(
            self.tr("&改变标签保存路径"),
            self.changeOutputDir,
            shortcuts["change_output_dir"],
            "ChangeLabelPath",
            self.tr("打开一个文件夹下所有的图像进行标注"),
        )
        quick_start = action(
            self.tr("&快速上手"),
            self.toBeImplemented,
            None,
            "Use",
            self.tr("快速上手介绍"),
        )
        about = action(
            self.tr("&关于软件"),
            self.toBeImplemented,
            None,
            "About",
            self.tr("关于这个软件和开发团队"),
        )
        grid_ann = action(
            self.tr("&N²宫格标注"),
            self.toBeImplemented,
            None,
            "N2",
            self.tr("使用N²宫格进行细粒度标注"),
        )
        finish_object = action(
            self.tr("&完成当前目标"),
            self.finishObject,
            shortcuts["finish_object"],
            "Ok",
            self.tr("完成当前目标的标注"),
        )
        clear = action(
            self.tr("&清除所有标注"),
            self.undoAll,
            shortcuts["clear"],
            "Clear",
            self.tr("清除所有标注信息"),
        )
        undo = action(
            self.tr("&撤销"),
            self.undoClick,
            shortcuts["undo"],
            "Undo",
            self.tr("撤销一次点击"),
        )
        redo = action(
            self.tr("&重做"),
            self.toBeImplemented,
            shortcuts["redo"],
            "Redo",
            self.tr("重做一次点击"),
        )
        save = action(
            self.tr("&保存"),
            self.saveLabel,
            "",
            "Save",
            self.tr("保存图像标签"),
        )
        save_as = action(
            self.tr("&另存为"),
            partial(self.saveLabel, True),
            "",
            "OtherSave",
            self.tr("指定标签保存路径"),
        )
        auto_save = action(
            self.tr("&自动保存"),
            self.toggleAutoSave,
            "",
            "AutoSave",
            self.tr("翻页同时自动保存"),
            checkable=True,
        )
        # auto_save.setChecked(self.config.get("auto_save", False))
        largest_component = action(
            self.tr("&保留最大连通块"),
            self.toggleLargestCC,
            "",
            "AutoSave",
            self.tr("翻页同时自动保存"),
            checkable=True,
        )
        recent = action(
            self.tr("&近期图片"),
            self.toBeImplemented,
            "",
            "RecentDocuments",
            self.tr("近期打开的图片"),
        )
        close = action(
            self.tr("&关闭"),
            self.toBeImplemented,
            "",
            "End",
            self.tr("关闭当前图像"),
        )
        connected = action(
            self.tr("&连通块"),
            self.toBeImplemented,
            "",
            # TODO: 搞个图
            "",
            self.tr(""),
        )
        quit = action(
            self.tr("&退出"),
            self.close,
            "",
            "Close",
            self.tr("退出软件"),
        )
        save_label = action(
            self.tr("&保存标签列表"),
            self.saveLabelList,
            "",
            "ExportLabel",
            self.tr("将标签保存成标签配置文件"),
        )
        load_label = action(
            self.tr("&加载标签列表"),
            self.loadLabelList,
            "",
            "ImportLabel",
            self.tr("从标签配置文件中加载标签"),
        )
        clear_label = action(
            self.tr("&清空标签列表"),
            self.clearLabelList,
            "",
            "ClearLabel",
            self.tr("清空所有的标签"),
        )
        shortcuts = action(
            self.tr("&快捷键列表"),
            self.toBeImplemented,
            "",
            "Shortcut",
            self.tr("查看所有快捷键"),
        )
        clear_recent = action(
            self.tr("&清除最近记录"),
            self.clearRecent,
            "",
            "ClearRecent",
            self.tr("删除最近记录文件"),
        )
        recent_files = QtWidgets.QMenu(self.tr("近期文件"))
        recent_files.aboutToShow.connect(self.updateFileMenu)
        recent_params = QtWidgets.QMenu(self.tr("近期模型及参数"))
        recent_params.aboutToShow.connect(self.updateModelsMenu)
        # TODO: 改用manager
        self.actions = util.struct(
            auto_save=auto_save,
            recent_files=recent_files,
            recent_params=recent_params,
            fileMenu=(
                open_image,
                open_folder,
                change_output_dir,
                # model_loader,
                clear_recent,
                recent_files,
                recent_params,
                None,
                save,
                save_as,
                auto_save,
                turn_next,
                turn_prev,
                close,
                None,
                quit,
            ),
            labelMenu=(
                save_label,
                load_label,
                clear_label,
                None,
                largest_component,
                grid_ann,
                del_active_polygon,
                del_active_point,
            ),
            helpMenu=(quick_start, about, shortcuts),
            toolBar=(finish_object, clear, undo, redo, turn_prev, turn_next),
        )
        menu("文件", self.actions.fileMenu)
        menu("标注", self.actions.labelMenu)
        menu("帮助", self.actions.helpMenu)
        util.addActions(self.toolBar, self.actions.toolBar)

    def queueEvent(self, function):
        # TODO: 研究这个东西是不是真的不影响ui
        QtCore.QTimer.singleShot(0, function)

    def showShortcuts(self):
        self.toBeImplemented()

    def toggleAutoSave(self, save):
        if save and not self.outputDir:
            self.changeOutputDir()
        if save and not self.outputDir:
            save = False
        self.actions.auto_save.setChecked(save)
        self.config["auto_save"] = save
        util.save_configs(osp.join(pjpath, "config/config.yaml"), self.config)

    def toggleLargestCC(self, on):
        self.filterLargestCC = on
        self.controller.filterLargestCC = on

    def changeModel(self, idx):
        self.modelClass = MODELS[idx]
        print("model class:", self.modelClass)

    def changeParam(self):
        formats = ["*.pdparams"]
        filters = self.tr("paddle model param files (%s)") % " ".join(formats)
        start_path = (
            "/home/lin/Downloads"
            if len(self.recentModels) == 0
            else osp.dirname(self.recentModels[-1]["param_path"])
        )
        # print(start_path)
        param_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("%s - 选择模型参数") % __APPNAME__,
            # "/home/lin/Downloads",
            start_path,
            filters,
        )
        if not osp.exists(param_path):
            return
        res = self.loadModelParam(param_path)
        if res:
            model_dict = {
                "param_path": param_path,
                "model_name": self.modelClass.__name__,
            }
            if model_dict not in self.recentModels:
                self.recentModels.append(model_dict)
                if len(self.recentModels) > 10:
                    del self.recentModels[0]
                self.settings.setValue("recent_models", self.recentModels)

    def loadModelParam(self, param_path, model=None):
        print("Call load model param: ", param_path, model, type(model))
        if model is None:
            model = self.modelClass()
        if isinstance(model, str):
            try:
                model = MODELS[model]()
            except KeyError:
                print("Model don't exist")
                return False
        if inspect.isclass(model):
            model = model()
        if not isinstance(model, models.EISegModel):
            print("not a instance")
            return False
        modelIdx = MODELS.idx(model.__name__)
        self.statusbar.showMessage(f"正在加载 {model.__name__} 模型")  # 这里没显示
        model = model.load_param(param_path)
        if model is not None:
            if self.controller is None:
                self.controller = InteractiveController(
                    model,
                    predictor_params={
                        # 'brs_mode': 'f-BRS-B',
                        "brs_mode": "NoBRS",
                        "prob_thresh": 0.5,
                        "zoom_in_params": {
                            "skip_clicks": -1,
                            "target_size": (400, 400),
                            "expansion_ratio": 1.4,
                        },
                        "predictor_params": {"net_clicks_limit": None, "max_size": 800},
                        "brs_opt_func_params": {"min_iou_diff": 0.001},
                        "lbfgs_params": {"maxfun": 20},
                    },
                    update_image_callback=self._update_image,
                )
                self.controller.prob_thresh = self.segThresh
                if self.image is not None:
                    self.controller.set_image(self.image)
            else:
                self.controller.reset_predictor(model)
            self.statusbar.showMessage(f"{osp.basename(param_path)} 模型加载完成", 20000)
            self.comboModelSelect.setCurrentIndex(modelIdx)
            return True
        else:  # 模型和参数不匹配
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("模型和参数不匹配")
            msg.setText("当前网络结构中的参数与模型参数不匹配，请更换网络结构或使用其他参数！")
            msg.setStandardButtons(QMessageBox.Yes)
            res = msg.exec_()
            self.statusbar.showMessage("模型和参数不匹配，请重新加载", 20000)
            self.controller = None  # 清空controller
            return False

    def loadRecentModelParam(self):
        if len(self.recentModels) == 0:
            self.statusbar.showMessage("没有最近使用模型信息，请加载模型", 10000)
            return
        m = self.recentModels[-1]
        model = MODELS[m["model_name"]]
        param_path = m["param_path"]
        self.loadModelParam(param_path, model)

    def clearRecent(self):
        self.settings.setValue("recent_files", [])
        # ini_path = osp.join(pjpath, "config/setting.ini")
        # print(ini_path)
        # if osp.exists(ini_path):
        #     os.remove(ini_path)
        self.statusbar.showMessage("已清除最近打开文件", 10000)

    def loadLabelList(self):
        filters = self.tr("标签配置文件 (*.txt)")
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("%s - 选择标签配置文件路径") % __APPNAME__,
            ".",
            filters,
        )
        if not osp.exists(file_path):
            return
        self.labelList.readLabel(file_path)
        print("Loaded label list:", self.labelList.list)
        self.refreshLabelList()
        self.settings.setValue("label_list_file", file_path)

    def saveLabelList(self):
        if len(self.labelList) == 0:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("没有需要保存的标签")
            msg.setText("请先添加标签之后再进行保存")
            msg.setStandardButtons(QMessageBox.Yes)
            res = msg.exec_()
            return
        filters = self.tr("标签配置文件 (*.txt)")
        dlg = QtWidgets.QFileDialog(self, "保存标签配置文件", ".", filters)
        dlg.setDefaultSuffix("txt")
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
        savePath, _ = dlg.getSaveFileName(
            self, self.tr("%s - 选择保存标签配置文件路径") % __APPNAME__, ".", filters
        )
        print("Save label list:", self.labelList.list, savePath)
        self.settings.setValue("label_list_file", savePath)
        self.labelList.saveLabel(savePath)

    def addLabel(self):
        c = self.maskColormap.get_color()
        table = self.labelListTable
        table.insertRow(table.rowCount())
        idx = table.rowCount() - 1
        self.labelList.add(idx + 1, "", c)
        print("append", self.labelList)
        numberItem = QTableWidgetItem(str(idx + 1))
        numberItem.setFlags(QtCore.Qt.ItemIsEnabled)
        table.setItem(idx, 0, numberItem)

        table.setItem(idx, 1, QTableWidgetItem())

        colorItem = QTableWidgetItem()
        colorItem.setBackground(QtGui.QColor(c[0], c[1], c[2]))
        colorItem.setFlags(QtCore.Qt.ItemIsEnabled)
        table.setItem(idx, 2, colorItem)

        delItem = QTableWidgetItem()
        delItem.setIcon(util.newIcon("Clear"))
        delItem.setTextAlignment(Qt.AlignCenter)
        delItem.setFlags(QtCore.Qt.ItemIsEnabled)
        table.setItem(idx, 3, delItem)

    def clearLabelList(self):
        self.labelList.clear()
        if self.controller:
            self.controller.label_list = []
            self.controller.curr_label_number = None
        self.labelListTable.clear()
        self.labelListTable.setRowCount(0)

    def refreshLabelList(self):
        table = self.labelListTable
        table.clearContents()
        table.setRowCount(len(self.labelList))
        table.setColumnCount(4)
        for idx, lab in enumerate(self.labelList):
            numberItem = QTableWidgetItem(str(lab.idx))
            numberItem.setFlags(QtCore.Qt.ItemIsEnabled)
            table.setItem(idx, 0, numberItem)
            table.setItem(idx, 1, QTableWidgetItem(lab.name))
            c = lab.color
            colorItem = QTableWidgetItem()
            colorItem.setBackground(QtGui.QColor(c[0], c[1], c[2]))
            colorItem.setFlags(QtCore.Qt.ItemIsEnabled)
            table.setItem(idx, 2, colorItem)
            delItem = QTableWidgetItem()
            delItem.setIcon(util.newIcon("clear"))
            delItem.setTextAlignment(Qt.AlignCenter)
            delItem.setFlags(QtCore.Qt.ItemIsEnabled)
            table.setItem(idx, 3, delItem)

        cols = [0, 1, 3]
        for idx in cols:
            table.resizeColumnToContents(idx)

    def labelListDoubleClick(self, row, col):
        print("Label list double clicked", row, col)
        if col != 2:
            return
        table = self.labelListTable
        color = QtWidgets.QColorDialog.getColor()
        if color.getRgb() == (0, 0, 0, 255):
            return
        print("Change to new color:", color.getRgb())
        table.item(row, col).setBackground(color)
        self.labelList[row].color = color.getRgb()[:3]
        if self.controller:
            self.controller.label_list = self.labelList
        for p in self.scene.polygon_items:
            p.setColor(self.labelList[p.labelIndex].color)

    @property
    def currLabelIdx(self):
        return self.controller.curr_label_number - 1

    def labelListClicked(self, row, col):
        print("cell clicked", row, col)
        table = self.labelListTable
        if col == 3:
            table.removeRow(row)
            self.labelList.remove(row)
        if col == 0 or col == 1:
            for idx in range(len(self.labelList)):
                table.item(idx, 0).setBackground(QtGui.QColor(255, 255, 255))
            table.item(row, 0).setBackground(QtGui.QColor(48, 140, 198))
            for idx in range(3):
                table.item(row, idx).setSelected(True)
            if self.controller:
                self.controller.change_label_num(int(table.item(row, 0).text()))
                self.controller.label_list = self.labelList

    def labelListItemChanged(self, row, col):
        print("cell changed", row, col)
        if col != 1:
            return
        name = self.labelListTable.item(row, col).text()
        self.labelList[row].name = name

    def openImage(self):
        formats = [
            "*.{}".format(fmt.data().decode())
            for fmt in QtGui.QImageReader.supportedImageFormats()
        ]
        filters = self.tr("Image & Label files (%s)") % " ".join(formats)
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("%s - 选择待标注图片") % __APPNAME__,
            "/home/lin/Desktop",
            filters,
        )
        if len(file_path) == 0:
            return
        self.queueEvent(partial(self.loadImage, file_path))
        self.listFiles.addItems([file_path])
        self.filePaths.append(file_path)
        # self.imagePath = file_path

    def loadLabel(self, imgPath):
        if imgPath == "" or len(self.labelPaths) == 0:
            return None

        def getName(path):
            return osp.basename(path).split(".")[0]

        imgName = getName(imgPath)
        for path in self.labelPaths:
            if getName(path) == imgName:
                labPath = path
                print(labPath)
                break
        label = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        print("label shape", label.shape)
        return label

    def loadImage(self, path, update_list=False):
        if len(path) == 0 or not osp.exists(path):
            return
        # TODO: 在不同平台测试含中文路径
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), 1)
        image = image[:, :, ::-1]  # BGR转RGB
        self.image = image
        self.currentPath = path
        if self.controller:
            self.controller.set_image(self.image)
        else:
            self.showWarning("未加载模型参数，请先加载模型参数！")
            self.changeParam()
            print("please load model params first!")
            return 0
        self.controller.set_label(self.loadLabel(path))
        if path not in self.recentFiles:
            self.recentFiles.append(path)
            if len(self.recentFiles) > 10:
                del self.recentFiles[0]
            self.settings.setValue("recent_files", self.recentFiles)
        self.imagePath = path  # 修复使用近期文件的图像保存label报错
        if update_list:
            self.listFiles.addItems([path])
            self.filePaths.append(path)

    def openFolder(self):
        self.inputDir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self.tr("%s - 选择待标注图片文件夹") % __APPNAME__,
            "/home/lin/Desktop",
            QtWidgets.QFileDialog.ShowDirsOnly
            | QtWidgets.QFileDialog.DontResolveSymlinks,
        )
        if len(self.inputDir) == 0:
            return
        filePaths = os.listdir(self.inputDir)
        exts = QtGui.QImageReader.supportedImageFormats()
        filePaths = [n for n in filePaths if n.split(".")[-1] in exts]
        filePaths = [osp.join(self.inputDir, n) for n in filePaths]
        self.filePaths += filePaths
        self.listFiles.addItems(filePaths)
        self.currIdx = 0
        self.turnImg(0)

    def listClicked(self):
        if self.controller.is_incomplete_mask:
            self.saveLabel()
        toRow = self.listFiles.currentRow()
        delta = toRow - self.currIdx
        self.turnImg(delta)

    def turnImg(self, delta):
        self.currIdx += delta
        if self.currIdx >= len(self.filePaths) or self.currIdx < 0:
            self.currIdx -= delta
            self.statusbar.showMessage(f"没有{'后一张'if delta==1 else '前一张'}图片")
            return
        self.completeLastMask()
        if self.isDirty:
            if self.actions.auto_save.isChecked():
                self.saveLabel()
            else:
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Warning)
                msg.setWindowTitle("保存标签？")
                msg.setText("标签尚未保存，是否保存标签")
                msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
                res = msg.exec_()
                if res == QMessageBox.Yes:
                    self.saveLabel()

        imagePath = self.filePaths[self.currIdx]
        self.loadImage(imagePath)
        self.imagePath = imagePath
        self.listFiles.setCurrentRow(self.currIdx)
        self.setClean()

    def finishObject(self):
        if not self.controller or self.image is None:
            return
        current_mask = self.controller.finish_object()
        if current_mask is not None:
            current_mask = current_mask.astype(np.uint8) * 255
            polygons = util.get_polygon(current_mask)
            self.setDirty()
            color = self.labelList[self.currLabelIdx].color
            for points in polygons:
                poly = PolygonAnnotation(self.currLabelIdx, color, color, self.opacity)
                poly.labelIndex = self.currLabelIdx
                self.scene.addItem(poly)
                self.scene.polygon_items.append(poly)
                for p in points:
                    poly.addPoint(QtCore.QPointF(p[0], p[1]))

    def completeLastMask(self):
        # 返回最后一个标签是否完成，false就是还有带点的
        if not self.controller:
            return True
        if not self.controller.is_incomplete_mask:
            return True
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("完成最后一个目标？")
        msg.setText("是否完成最后一个目标的标注，不完成不会进行保存。")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        res = msg.exec_()
        if res == QMessageBox.Yes:
            self.finishObject()
            self.setDirty()
            return True
        return False

    def saveLabel(self, saveAs=False, savePath=None):
        if not self.controller or self.controller.image is None:
            return
        self.completeLastMask()
        if not savePath:  # 参数没传存到哪
            if not saveAs and self.outputDir is not None:
                # 指定了标签文件夹，而且不是另存为
                savePath = osp.join(
                    self.outputDir, osp.basename(self.imagePath).split(".")[0] + ".png"
                )
            else:
                filters = self.tr("Label files (*.png)")
                dlg = QtWidgets.QFileDialog(
                    self, "保存标签文件路径", osp.dirname(self.imagePath), filters
                )
                dlg.setDefaultSuffix("png")
                dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
                dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
                dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
                savePath, _ = dlg.getSaveFileName(
                    self,
                    self.tr("选择标签文件保存路径"),
                    osp.basename(self.imagePath).split(".")[0] + ".png",
                )
        if (
            savePath is None
            or len(savePath) == 0
            or not osp.exists(osp.dirname(savePath))
        ):
            return

        cv2.imwrite(savePath, self.controller.result_mask)
        # 保存路径带有中文
        # cv2.imencode('.png', self.controller.result_mask)[1].tofile(savePath)
        # 保存带有调色板的
        # mask_pil = Image.fromarray(self.controller.result_mask, "P")
        # mask_map = [0, 0, 0]
        # for lb in self.labelList:
        #     mask_map += lb[2]
        # mask_pil.putpalette(mask_map)
        # mask_pil.save(savePath)
        # self.setClean()
        self.statusbar.showMessage(f"标签成功保存至 {savePath}")

    def setClean(self):
        self.isDirty = False

    def setDirty(self):
        self.isDirty = True

    def changeOutputDir(self):
        outputDir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self.tr("%s - 选择标签保存路径") % __APPNAME__,
            # osp.dirname(self.imagePath),
            ".",
            QtWidgets.QFileDialog.ShowDirsOnly
            | QtWidgets.QFileDialog.DontResolveSymlinks,
        )
        if len(outputDir) == 0 or not osp.exists(outputDir):
            return False
        labelPaths = os.listdir(outputDir)
        exts = ["png"]
        labelPaths = [n for n in labelPaths if n.split(".")[-1] in exts]
        labelPaths = [osp.join(outputDir, n) for n in labelPaths]
        self.outputDir = outputDir
        self.labelPaths = labelPaths
        return True

    def maskOpacityChanged(self):
        self.sldOpacity.textLab.setText(str(self.opacity))
        if not self.controller or self.controller.image is None:
            return
        for polygon in self.scene.polygon_items:
            polygon.setOpacity(self.opacity)
        self._update_image()

    def clickRadiusChanged(self):
        self.sldClickRadius.textLab.setText(str(self.clickRadius))
        if not self.controller or self.controller.image is None:
            return

        self._update_image()

    def threshChanged(self):
        self.sldThresh.textLab.setText(str(self.segThresh))
        if not self.controller or self.controller.image is None:
            return
        self.controller.prob_thresh = self.segThresh
        self._update_image()

    def undoClick(self):
        if self.image is None:
            return
        if not self.controller:
            return
        self.controller.undo_click()
        if not self.controller.is_incomplete_mask:
            self.setClean()

    def undoAll(self):
        if not self.controller or self.controller.image is None:
            return
        self.controller.reset_last_object()
        self.setClean()

    def redoClick(self):
        self.toBeImplemented()

    def canvasClick(self, x, y, isLeft):
        if self.controller is None:
            return
        if self.controller.image is None:
            return
        currLabel = self.controller.curr_label_number
        if not currLabel or currLabel == 0:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("未选择当前标签")
            msg.setText("请先在标签列表中单击点选标签")
            msg.setStandardButtons(QMessageBox.Yes)
            res = msg.exec_()
            return

        self.controller.add_click(x, y, isLeft)

    def _update_image(self, reset_canvas=False):
        if not self.controller:
            return
        image = self.controller.get_visualization(
            alpha_blend=self.opacity,
            click_radius=self.clickRadius,
        )
        height, width, channel = image.shape
        bytesPerLine = 3 * width
        image = QImage(image.data, width, height, bytesPerLine, QImage.Format_RGB888)
        if reset_canvas:
            self.resetZoom(width, height)
        self.img_item.setPixmap(QPixmap(image))

        # BUG: 一直有两张图片在scene里，研究是为什么
        # print(self.scene.items())

    # 界面缩放重置
    def resetZoom(self, width, height):
        # 每次加载图像前设定下当前的显示框，解决图像缩小后不在中心的问题
        self.scene.setSceneRect(0, 0, width, height)
        # 缩放清除
        self.canvas.scale(1 / self.canvas.zoom_all, 1 / self.canvas.zoom_all)  # 重置缩放
        self.canvas.zoom_all = 1
        # 最佳缩放
        s_eps = 5e-2
        scr_cont = [
            self.scrollArea.width() / width - s_eps,
            self.scrollArea.height() / height - s_eps,
        ]
        if scr_cont[0] * height > self.scrollArea.height():
            self.canvas.zoom_all = scr_cont[1]
        else:
            self.canvas.zoom_all = scr_cont[0]
        self.canvas.scale(self.canvas.zoom_all, self.canvas.zoom_all)

    @property
    def opacity(self):
        return self.sldOpacity.value() / 100

    @property
    def clickRadius(self):
        return self.sldClickRadius.value()

    @property
    def segThresh(self):
        return self.sldThresh.value() / 100

    # 警告框
    def showWarning(self, str):
        msg_box = QMessageBox(QMessageBox.Warning, "警告", str)
        msg_box.exec_()

    def toBeImplemented(self):
        self.statusbar.showMessage("功能尚在开发")
