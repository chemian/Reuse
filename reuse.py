import sys
import sqlite3
import os
import keyboard
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, 
                            QWidget, QTableWidget, QTableWidgetItem, QLineEdit, QVBoxLayout, 
                            QMessageBox, QInputDialog, QHeaderView, QAbstractItemView, QSplitter, 
                            QTextEdit, QFrame, QSizePolicy, QShortcut, QDialog)
from PyQt5.QtGui import (QKeySequence, QIcon, QFont, QColor, QBrush, QTextOption, QTextCursor, QCursor)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QEvent, QObject

class WorkerSignals(QObject):
    show_window = pyqtSignal()

class PreviewDialog(QDialog):
    """预览悬浮窗 - 宽度与主窗口一致，高度自适应内容"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.MinimumExpanding)
        
        # 布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # 预览区域
        self.preview_area = QTextEdit()
        self.preview_area.setReadOnly(True)
        self.preview_area.setFrameShape(QFrame.NoFrame)
        self.preview_area.setStyleSheet("""
            QTextEdit {
                background-color: #F8F8F8;
                border: 1px solid #E0E0E0;
                padding: 8px;
                font-size: 12px;
                color: #333333;
                border-radius: 3px;
            }
        """)
        # 设置字体为等宽字体
        font = QFont("Consolas")
        font.setPointSize(10)
        self.preview_area.setFont(font)
        
        # 启用滚动和自动换行
        self.preview_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.preview_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preview_area.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        
        layout.addWidget(self.preview_area)
    
    def set_content(self, content, row_num):
        """设置预览内容并自动调整高度"""
        preview_text = f"记录 #{row_num} 预览:\n\n{content}"
        self.preview_area.setText(preview_text)
        
        # 计算理想高度
        doc = self.preview_area.document()
        doc.adjustSize()
        
        # 获取一行文本的理想高度
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.Start)
        rect = self.preview_area.cursorRect(cursor)
        line_height = rect.height()
        
        # 计算总行数
        line_count = doc.lineCount()
        
        # 计算理想高度（行数 * 行高 + 边距）
        ideal_height = line_count * line_height + 40
        
        # 限制最大高度不超过主窗口高度
        max_height = self.parent().height() if self.parent() else 500
        self.resize(self.width(), min(ideal_height, max_height))

class ReuseDatabase:
    """管理剪贴板历史记录的数据库"""
    def __init__(self, db_path='reuse_history.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.create_table()
        print(f"数据库文件: {os.path.abspath(db_path)}")
    
    def create_table(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS clips (
                        id INTEGER PRIMARY KEY,
                        content TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        self.conn.commit()
    
    def save_clip(self, content):
        """保存新的剪贴板内容"""
        if not content or content.isspace():
            return False
            
        try:
            # 检查是否已存在相同内容
            cursor = self.conn.execute("SELECT id FROM clips WHERE content = ?", (content,))
            if cursor.fetchone():
                return False
                
            self.conn.execute("INSERT INTO clips (content) VALUES (?)", (content,))
            self.conn.commit()
            print(f"保存新内容: {content[:50]}{'...' if len(content) > 50 else ''}")
            return True
        except sqlite3.Error as e:
            print(f"数据库保存错误: {e}")
            return False
    
    def get_all_clips(self, limit=200):
        try:
            cursor = self.conn.execute(
                "SELECT id, content, timestamp FROM clips "
                "ORDER BY id DESC LIMIT ?", 
                (limit,))
            clips = cursor.fetchall()
            print(f"从数据库加载 {len(clips)} 条记录")
            return clips
        except sqlite3.Error as e:
            print(f"数据库查询错误: {e}")
            return []
    
    def search_clips(self, keyword, limit=100):
        try:
            cursor = self.conn.execute(
                "SELECT id, content, timestamp FROM clips "
                "WHERE content LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                ('%' + keyword + '%', limit)
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            print(f"数据库搜索错误: {e}")
            return []
    
    def delete_clip(self, clip_id):
        """删除指定ID的记录""" 
        self.conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        self.conn.commit()
    
    def clear_all(self):
        """清空所有记录（保留置顶项）"""
        self.conn.execute("DELETE FROM clips")
        self.conn.commit()
    
    def set_limit(self, limit):
        """设置历史记录最大数量并保留最新记录"""
        self.conn.execute(
            "DELETE FROM clips WHERE id NOT IN ("
            "  SELECT id FROM clips "
            "  ORDER BY id DESC LIMIT ?"
            ")", (limit,))
        self.conn.commit()

    def update_clip_as_latest(self, clip_id):
        """将指定ID的内容更新为最新记录（删除后重新插入）"""
        cursor = self.conn.execute("SELECT content FROM clips WHERE id = ?", (clip_id,))
        result = cursor.fetchone()
        if not result:
            return False

        content = result[0]
        self.conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        self.conn.execute("INSERT INTO clips (content) VALUES (?)", (content,))
        self.conn.commit()
        return True

class ReuseHistoryWindow(QWidget):
    """剪贴板历史记录主窗口 - 使用悬浮窗预览"""
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.current_limit = 200  # 默认记录数
        self.current_preview_row = -1  # 当前预览的行
        self.preview_dialog = None  # 预览悬浮窗
        self.hide_timer = QTimer(self)  # 用于延迟隐藏预览框
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_preview)
        self.setFocusPolicy(Qt.StrongFocus)

        # 添加快捷键 ESC（用于关闭）
        self.shortcut_esc = QShortcut(QKeySequence("Esc"), self)
        self.shortcut_esc.activated.connect(self.close_window)

        self.init_ui()
        self.refresh_clips()

    def close_window(self):
        """关闭窗口"""
        print("窗口已关闭")
        self.close()


    def init_ui(self):
        # 窗口设置 - 固定宽度为800像素（更符合实际需求）
        self.setWindowTitle('Reuse')
        self.setGeometry(300, 300, 400, 600)
        self.setWindowIcon(QIcon('reuse.ico'))
        
        # 主布局
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        
        # 创建搜索区域
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索剪贴板历史...")
        self.search_box.textChanged.connect(self.search_clips)
        
        # 创建历史表格
        self.table_widget = QTableWidget()
        self.table_widget.setColumnCount(3)  # 序号、内容、时间
        self.table_widget.setHorizontalHeaderLabels(['序号', '内容', '时间'])
        
        # 优化表格样式
        self.table_widget.setShowGrid(False)
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
        
        # 设置列宽策略
        self.table_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table_widget.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table_widget.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        
        # 设置行高
        self.table_widget.verticalHeader().setDefaultSectionSize(30)
        
        # 设置斑马纹样式
        self.table_widget.setAlternatingRowColors(True)
        self.table_widget.setStyleSheet("""
            QTableWidget {
                background-color: #FFFFFF;
                alternate-background-color: #F5F5F5;
                font-size: 12px;
                gridline-color: transparent;
            }
            QTableWidget::item {
                padding: 5px;
                border: none;
            }
            QHeaderView::section {
                background-color: #F0F0F0;
                padding: 5px;
                border: none;
                font-weight: bold;
            }
            /* 修复选中行样式 */
            QTableWidget::item:selected {
                background-color: #4A90E2;
                color: white;
            }
        """)
        
        # 连接事件
        self.table_widget.cellDoubleClicked.connect(self.copy_to_clipboard)
        self.table_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_widget.customContextMenuRequested.connect(self.show_context_menu)
        
        # 新的悬停预览机制
        self.table_widget.setMouseTracking(True)
        self.table_widget.entered.connect(self.handle_cell_entered)
        self.table_widget.viewport().installEventFilter(self)
        
        # 创建分割器
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table_widget)
        
        # 布局
        main_layout.addWidget(self.search_box)
        main_layout.addWidget(splitter)
        
        self.setLayout(main_layout)
    
    def eventFilter(self, source, event):
        """事件过滤器用于检测鼠标离开表格和预览框事件"""
        # 鼠标离开表格区域
        if source is self.table_widget.viewport() and event.type() == QEvent.Leave:
            # 使用延迟隐藏，给用户移动到预览窗口的时间
            self.hide_timer.start(300)  # 300毫秒后隐藏
        
        # 预览框相关事件
        elif self.preview_dialog and source is self.preview_dialog:
            # 鼠标进入预览框
            if event.type() == QEvent.Enter:
                # 停止隐藏计时器 - 用户正在使用预览框
                self.hide_timer.stop()
                return True
            # 鼠标离开预览框
            elif event.type() == QEvent.Leave:
                # 启动延迟隐藏
                self.hide_timer.start(300)
                return True
        
        return super().eventFilter(source, event)
    
    def handle_cell_entered(self, index):
        """当鼠标进入单元格时触发预览"""
        self.hide_timer.stop()  # 停止延迟隐藏
        row = index.row()
        if row != self.current_preview_row:
            self.current_preview_row = row
            self.show_preview(row)
    
    def show_preview(self, row):
        """显示悬浮窗预览"""
        if row < 0 or row >= self.table_widget.rowCount():
            self.hide_preview()
            return
        
        # 获取内容项
        content_item = self.table_widget.item(row, 1)
        if not content_item:
            self.hide_preview()
            return
        
        # 获取完整内容
        clip_data = content_item.data(Qt.UserRole)
        if not clip_data:
            self.hide_preview()
            return
        
        content = clip_data.get("full_content", "")
        
        # 创建或更新预览窗口
        if not self.preview_dialog:
            self.preview_dialog = PreviewDialog(self)
            # 设置预览框事件过滤器
            self.preview_dialog.installEventFilter(self)
        
        # 设置预览窗口宽度与主窗口一致
        self.preview_dialog.setFixedWidth(self.width())
        
        # 设置内容并自动调整高度
        self.preview_dialog.set_content(content, row + 1)
        
        # 获取当前鼠标位置
        mouse_pos = QCursor.pos()
        
        # 移动预览窗口到鼠标位置
        self.preview_dialog.move(mouse_pos.x() + 20, mouse_pos.y() + 20)
        
        # 确保预览窗口不会超出屏幕
        screen = QApplication.desktop().screenGeometry()
        preview_rect = self.preview_dialog.frameGeometry()
        
        # 如果预览窗右边超出屏幕右边，向左调整
        if preview_rect.right() > screen.right():
            self.preview_dialog.move(screen.right() - preview_rect.width() - 10, preview_rect.top())
        
        # 如果预览窗底部超出屏幕底部，向上调整
        if preview_rect.bottom() > screen.bottom():
            self.preview_dialog.move(preview_rect.left(), screen.bottom() - preview_rect.height() - 10)
        
        # 如果预览窗左边超出屏幕左边，向右调整
        if preview_rect.left() < screen.left():
            self.preview_dialog.move(screen.left() + 10, preview_rect.top())
        
        # 如果预览窗顶部超出屏幕顶部，向下调整
        if preview_rect.top() < screen.top():
            self.preview_dialog.move(preview_rect.left(), screen.top() + 10)
        
        self.preview_dialog.show()
    
    def hide_preview(self):
        """隐藏预览窗口"""
        if self.preview_dialog and self.preview_dialog.isVisible():
            self.preview_dialog.hide()
            self.current_preview_row = -1
        
    def refresh_clips(self):
        clips = self.db.get_all_clips(self.current_limit)
        self.load_clips(clips)
        self.hide_preview()

        if self.table_widget.rowCount() > 0:
            self.table_widget.selectRow(0)
        
        # 增加这行：保持焦点在搜索框
        self.search_box.setFocus()
    
    def load_clips(self, clips):
        """加载剪贴板记录到表格"""
        self.table_widget.setRowCount(0)  # 清空表格

        if not clips:
            self.table_widget.setRowCount(1)
            item = QTableWidgetItem("没有找到剪贴板历史记录")
            item.setTextAlignment(Qt.AlignCenter)
            self.table_widget.setItem(0, 1, item)
            self.table_widget.setSpan(0, 0, 1, 3)
            return

        # 设置行数
        self.table_widget.setRowCount(len(clips))

        # 斑马纹颜色
        color1 = QColor(255, 255, 255)  # 白色
        color2 = QColor(245, 245, 245)  # 浅灰色

        for row, (clip_id, content, timestamp) in enumerate(clips):
            # 设置行背景色（斑马纹效果）
            bg_color = color1 if row % 2 == 0 else color2
            text_color = QColor(0, 0, 0)  # 黑色文字

            # 创建行项
            for col in range(3):
                item = QTableWidgetItem()
                item.setBackground(QBrush(bg_color))
                item.setForeground(QBrush(text_color))
                self.table_widget.setItem(row, col, item)

            # 序号列
            seq_item = self.table_widget.item(row, 0)
            seq_item.setText(f"{row + 1}")
            seq_item.setData(Qt.UserRole, clip_id)
            seq_item.setTextAlignment(Qt.AlignCenter)

            # 内容列
            display_text = content if len(content) <= 80 else content[:80] + "..."

            content_item = self.table_widget.item(row, 1)
            content_item.setText(display_text)
            content_item.setData(Qt.UserRole, {
                "id": clip_id,
                "content": content,
                "full_content": content
            })

            # 时间列
            time_item = self.table_widget.item(row, 2)
            time_item.setText(timestamp)
    
    def search_clips(self, keyword):
        if keyword:
            clips = self.db.search_clips(keyword, self.current_limit)
            self.load_clips(clips)
            
            self.search_box.setFocus()
            
            if self.table_widget.rowCount() > 0:
                self.table_widget.selectRow(0)
        else:
            self.refresh_clips()  # 现在不会跳转焦点
    
    def copy_to_clipboard(self, row, column):
        """将选中项复制回剪贴板，并刷新为最新记录"""
        if row < 0 or row >= self.table_widget.rowCount():
            return

        item = self.table_widget.item(row, 1)
        if item:
            clip_data = item.data(Qt.UserRole)
            if clip_data:
                content = clip_data["content"]
                clip_id = clip_data["id"]

                # 更新数据库：设为最新记录
                self.db.update_clip_as_latest(clip_id)

                # 设置剪贴板内容
                clipboard = QApplication.clipboard()
                clipboard.setText(content)

                # 关闭窗口
                self.close_window()

                # 粘贴内容到之前焦点位置
                QTimer.singleShot(100, lambda: self.paste_to_focus(content))
    
    def paste_to_focus(self, content):
        import win32con
        import win32api
        import time

        try:
            # 使用 PyQt 设置剪贴板内容（更安全）
            clipboard = QApplication.clipboard()
            clipboard.setText(content)

            # 延迟一点让系统准备就绪
            time.sleep(0.1)

            # 模拟 Ctrl+V 粘贴
            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            win32api.keybd_event(ord('V'), 0, 0, 0)
            time.sleep(0.02)
            win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

            self.show_notification("已粘贴", "内容已粘贴到目标应用")
            
        except Exception as e:
            print("粘贴失败:", e)
            self.show_notification("粘贴失败", str(e))
    
    def show_context_menu(self, position):
        """显示右键菜单"""
        index = self.table_widget.indexAt(position)
        if not index.isValid():
            return
            
        row = index.row()
        item = self.table_widget.item(row, 1)
        if not item:
            return
            
        clip_data = item.data(Qt.UserRole)
        if not clip_data:
            return
            
        clip_id = clip_data["id"]
        
        menu = QMenu()
        
        # 删除
        delete_action = menu.addAction("删除")
        delete_action.triggered.connect(lambda: self.delete_clip(clip_id, row))
        
        # 复制完整内容
        copy_action = menu.addAction("复制完整内容")
        copy_action.triggered.connect(lambda: self.copy_full_content(clip_data["content"]))
        
        menu.exec_(self.table_widget.viewport().mapToGlobal(position))
    
    def toggle_pin_status(self, clip_id, row):
        """切换置顶状态"""
        new_state = self.db.toggle_pin(clip_id)
        
        # 更新UI样式
        if new_state:
            color = QColor(255, 255, 200)  # 置顶背景色
            text_color = QColor(200, 0, 0)  # 红色文字
        else:
            # 根据行号恢复斑马纹颜色
            color = QColor(255, 255, 255) if row % 2 == 0 else QColor(245, 245, 245)
            text_color = QColor(0, 0, 0)  # 黑色文字
            
        for col in range(3):
            item = self.table_widget.item(row, col)
            if item:
                item.setBackground(QBrush(color))
                item.setForeground(QBrush(text_color))
        
        # 更新内容列的置顶标识
        content_item = self.table_widget.item(row, 1)
        if content_item:
            clip_data = content_item.data(Qt.UserRole)
            if clip_data:
                clip_data["pinned"] = bool(new_state)
                content_item.setData(Qt.UserRole, clip_data)
                
                prefix = "📌 " if new_state else ""
                current_text = content_item.text()
                if current_text.startswith("📌 "):
                    content_item.setText(prefix + current_text[2:])
                else:
                    content_item.setText(prefix + current_text)
        
        self.show_notification("已置顶" if new_state else "已取消置顶", "操作成功")
    
    def delete_clip(self, clip_id, row):
        """删除单个记录"""
        self.db.delete_clip(clip_id)
        self.table_widget.removeRow(row)
        self.hide_preview()
        self.show_notification("已删除", "记录已移除")
    
    def copy_full_content(self, content):
        """复制完整内容到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(content)
        self.show_notification("已复制完整内容", content[:50] + "..." if len(content) > 50 else content)
    
    def confirm_clear(self):
        """确认清空历史记录"""
        reply = QMessageBox.question(
            self, '确认清空',
            '确定要清空所有非置顶的剪贴板历史吗？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.db.clear_all()
            self.refresh_clips()
            self.show_notification("已清空", "非置顶记录已清除")
    
    def open_settings(self):
        """打开设置对话框"""
        new_limit, ok = QInputDialog.getInt(
            self, '设置历史记录数量',
            '最大保存记录数 (20-500):',
            self.current_limit, 20, 500, 10
        )
        
        if ok:
            self.current_limit = new_limit
            self.db.set_limit(new_limit)
            self.refresh_clips()
            self.show_notification("设置已更新", f"将保存最多 {new_limit} 条记录")
    
    def show_notification(self, title, message):
        """显示操作反馈通知"""
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(title)
        msg.setText(message)
        msg.setStandardButtons(QMessageBox.Ok)
        msg.show()
        
        # 2秒后自动关闭
        QTimer.singleShot(2000, msg.close)

    def keyPressEvent(self, event):
        """处理键盘按键事件"""
        if event.key() == Qt.Key_Enter or event.key() == Qt.Key_Return:
            current_row = self.table_widget.currentRow()
            if 0 <= current_row < self.table_widget.rowCount():
                content_item = self.table_widget.item(current_row, 1)
                if content_item:
                    clip_data = content_item.data(Qt.UserRole)
                    if clip_data and "content" in clip_data:
                        content = clip_data["content"]
                        clip_id = clip_data["id"]

                        # 设置剪贴板内容
                        clipboard = QApplication.clipboard()
                        clipboard.setText(content)

                        # 关闭窗口
                        self.close_window()

                        # 更新数据库：设为最新记录
                        self.db.update_clip_as_latest(clip_id)
                        
                        # 粘贴内容到之前焦点位置
                        QTimer.singleShot(100, lambda: self.paste_to_focus(content))
                    else:
                        self.show_notification("错误", "未找到可粘贴内容")
            else:
                self.show_notification("错误", "请选择一行后再按回车")
        else:
            super().keyPressEvent(event)
    def showEvent(self, event):
        super().showEvent(event)
        self.search_box.setFocus()

class ReuseManager:
    """剪贴板管理核心类"""
    def __init__(self):
        self.db = ReuseDatabase()
        self.last_clipboard_content = ""
        
        # 创建历史窗口
        self.history_window = ReuseHistoryWindow(self.db)
        
        # 初始化系统托盘
        self.tray_icon = QSystemTrayIcon()
        self.init_tray_icon()
        
        # 监听剪贴板变化
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.handle_clipboard_change)
        
        # 初始化时立即检查一次剪贴板
        QTimer.singleShot(1000, self.handle_clipboard_change)
        
        # 调试：确保剪贴板监控已连接
        print("剪贴板监控已连接:", self.clipboard.receivers(self.clipboard.dataChanged) > 0)

        self.signals = WorkerSignals()
        # 绑定主窗口显示逻辑
        self.signals.show_window.connect(self.show_history_window)
        # 注册快捷键
        self.register_hotkey()
    def register_hotkey(self):
        """注册全局快捷键"""
        try:
            keyboard.unhook_all_hotkeys()  # 清除之前的快捷键
        except:
            pass
        def on_hotkey():
            self.signals.show_window.emit()  # 安全地发送信号到主线程

        keyboard.add_hotkey('ctrl+shift+q', on_hotkey)
        print("快捷键已注册: Ctrl+Shift+Q")
    
    def init_tray_icon(self):
        # 创建托盘图标
        if os.path.exists('reuse.ico'):
            self.tray_icon.setIcon(QIcon('reuse.ico'))
        else:
            # 使用默认图标
            self.tray_icon.setIcon(QApplication.style().standardIcon(QApplication.style().SP_ComputerIcon))
            print("使用默认系统托盘图标")
        
        # 创建右键菜单
        tray_menu = QMenu()
        
        show_action = tray_menu.addAction("显示历史")
        show_action.triggered.connect(self.show_history_window)
        
        settings_action = tray_menu.addAction("设置")
        settings_action.triggered.connect(self.history_window.open_settings)
        
        tray_menu.addSeparator()
        
        exit_action = tray_menu.addAction("退出")
        exit_action.triggered.connect(QApplication.quit)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.tray_icon_activated)
        self.tray_icon.show()
        print("系统托盘图标已初始化")
    
    def tray_icon_activated(self, reason):
        """托盘图标点击处理"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_history_window()
    
    def show_history_window(self):
        """显示历史记录窗口"""
        # 每次显示窗口时刷新数据
        self.history_window.refresh_clips()
        # 先恢复窗口状态（防止最小化）
        if self.history_window.isMinimized():
            self.history_window.setWindowState(Qt.WindowNoState)

        # 显示窗口
        self.history_window.show()

        # 设置为激活状态（关键！）
        self.history_window.setWindowState(Qt.WindowActive)
        
        # 再次确保置顶并激活
        self.history_window.raise_()
        self.history_window.activateWindow()

        # 使用 Windows API 强行激活
        try:
            hwnd = int(self.history_window.winId())
            QTimer.singleShot(100, lambda: self.activate_window(hwnd))
        except:
            pass
        print("显示历史窗口并强制激活")
    @staticmethod
    def activate_window(hwnd):
        import win32gui
        import win32con
        import win32api

        # 模拟一次鼠标移动/点击动作，绕过 Windows 的激活限制
        try:
            # 发送一个空的鼠标移动事件，欺骗系统这是一个“用户行为”
            win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, 0, 0, 0, 0)
            
            # 恢复窗口（如果最小化）
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            
            # 设置为前台窗口
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            print("激活窗口失败:", e)
    def handle_clipboard_change(self):
        """处理剪贴板内容变化"""
        try:
            # 获取文本内容
            if self.clipboard.mimeData().hasText():
                new_content = self.clipboard.text()
                print(f"剪贴板变化: {new_content[:50]}{'...' if len(new_content) > 50 else ''}")
                
                # 忽略空内容和重复内容
                if new_content and new_content != self.last_clipboard_content:
                    print(f"检测到新内容: {new_content[:50]}{'...' if len(new_content) > 50 else ''}")
                    self.last_clipboard_content = new_content
                    
                    # 保存到数据库
                    if self.db.save_clip(new_content):
                        print("内容已保存到数据库")
                        # 如果历史窗口正在显示，刷新它
                        if self.history_window.isVisible():
                            self.history_window.refresh_clips()
                            print("刷新历史窗口")
        except Exception as e:
            print(f"剪贴板处理错误: {e}")

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    # 设置工作目录
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"当前工作目录: {os.getcwd()}")
    
    # Windows应用ID设置
    if sys.platform == 'win32':
        try:
            import ctypes
            app_id = 'com.yourcompany.clipboardenhancer.1.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
            print("已设置应用ID")
        except Exception as e:
            print(f"设置应用ID失败: {e}")
    
    # 启动管理器
    manager = ReuseManager()
    print("剪贴板管理器已启动")
    
    sys.exit(app.exec_())

if __name__ == '__main__':
    # 确保图标文件存在
    if not os.path.exists('reuse.ico'):
        print("警告: 未找到reuse.ico文件，将使用默认图标")
    
    # 添加详细的启动日志
    print("=" * 60)
    print("剪贴板增强工具启动")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python版本: {sys.version}")
    print(f"工作目录: {os.getcwd()}")
    print("=" * 60)
    
    main()