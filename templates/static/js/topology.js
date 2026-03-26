/**
 * 服务拓扑模块
 * 管理服务依赖拓扑的可视化和编辑
 */

const TopologyModule = {
    topology: null,
    nodes: [],
    edges: [],

    /**
     * 初始化模块
     */
    init() {
        this.loadTopology();
        this.bindEvents();
    },

    /**
     * 绑定事件
     */
    bindEvents() {
        // 添加依赖按钮
        const addDependencyBtn = document.getElementById('addDependencyBtn');
        if (addDependencyBtn) {
            addDependencyBtn.addEventListener('click', () => this.addDependency());
        }

        // 添加服务节点按钮
        const addServiceNodeBtn = document.getElementById('addServiceNodeBtn');
        if (addServiceNodeBtn) {
            addServiceNodeBtn.addEventListener('click', () => this.addServiceNode());
        }

        // 自动发现按钮
        const discoverBtn = document.getElementById('discoverTopologyBtn');
        if (discoverBtn) {
            discoverBtn.addEventListener('click', () => this.discoverTopology());
        }

        // 拓扑图点击事件委托
        const topologyContainer = document.getElementById('topologyGraph');
        if (topologyContainer) {
            topologyContainer.addEventListener('click', (e) => {
                const deleteBtn = e.target.closest('[data-delete-edge]');
                if (deleteBtn) {
                    const source = deleteBtn.getAttribute('data-source');
                    const target = deleteBtn.getAttribute('data-target');
                    this.deleteDependency(source, target);
                }
            });
        }
    },

    /**
     * 加载拓扑数据
     */
    async loadTopology() {
        try {
            const result = await API.getTopology();

            if (result.success && result.data) {
                this.topology = result.data;
                this.nodes = result.data.nodes || [];
                this.edges = result.data.edges || [];
                this.renderTopology(this.topology);
            } else {
                this.renderEmptyTopology();
            }
        } catch (error) {
            console.error('加载拓扑失败:', error);
            this.renderEmptyTopology();
        }
    },

    /**
     * 渲染拓扑图
     * @param {object} topology - 拓扑数据
     */
    renderTopology(topology) {
        const container = document.getElementById('topologyGraph');
        if (!container) return;

        const nodes = topology.nodes || [];
        const edges = topology.edges || [];

        if (nodes.length === 0) {
            this.renderEmptyTopology();
            return;
        }

        // 简化的拓扑渲染 - 使用列表形式展示
        let html = '<div class="topology-container">';

        // 节点列表
        html += '<div class="topology-nodes">';
        html += '<h4>服务节点 (' + nodes.length + ')</h4>';
        html += '<div class="nodes-grid">';

        nodes.forEach((node) => {
            const nodeType = node.type || 'service';
            const healthStatus = node.health || 'unknown';

            html += '<div class="topology-node ' + healthStatus + '" data-node-id="' + node.id + '">';
            html += '<div class="node-icon">' + this.getNodeIcon(nodeType) + '</div>';
            html += '<div class="node-info">';
            html += '<div class="node-name">' + node.name + '</div>';
            html += '<div class="node-type">' + nodeType + '</div>';
            html += '</div>';
            html += '<div class="node-status ' + healthStatus + '"></div>';
            html += '</div>';
        });

        html += '</div>';
        html += '</div>';

        // 依赖关系列表
        if (edges.length > 0) {
            html += '<div class="topology-edges">';
            html += '<h4>依赖关系 (' + edges.length + ')</h4>';
            html += '<div class="edges-list">';

            edges.forEach((edge) => {
                const sourceNode = nodes.find(n => n.id === edge.source);
                const targetNode = nodes.find(n => n.id === edge.target);

                html += '<div class="edge-item">';
                html += '<span class="edge-source">' + (sourceNode ? sourceNode.name : edge.source) + '</span>';
                html += '<span class="edge-arrow">→</span>';
                html += '<span class="edge-target">' + (targetNode ? targetNode.name : edge.target) + '</span>';
                html += '<span class="edge-type">' + (edge.type || 'depends_on') + '</span>';
                html += '<button class="btn btn-sm btn-danger" data-delete-edge data-source="' + edge.source + '" data-target="' + edge.target + '">删除</button>';
                html += '</div>';
            });

            html += '</div>';
            html += '</div>';
        }

        html += '</div>';
        container.innerHTML = html;
    },

    /**
     * 获取节点图标
     * @param {string} type - 节点类型
     * @returns {string} 图标字符
     */
    getNodeIcon(type) {
        const icons = {
            'service': '🟦',
            'database': '🗄️',
            'cache': '⚡',
            'queue': '📬',
            'gateway': '🚪',
            'loadbalancer': '⚖️',
            'unknown': '❓'
        };
        return icons[type] || icons['service'];
    },

    /**
     * 渲染空拓扑状态
     */
    renderEmptyTopology() {
        const container = document.getElementById('topologyGraph');
        if (!container) return;

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">🕸️</div><div class="empty-title">暂无拓扑数据</div><div class="empty-text">点击"自动发现"按钮开始扫描</div></div>';
    },

    /**
     * 添加服务节点
     */
    async addServiceNode() {
        const serviceName = prompt('请输入服务名称:');
        if (!serviceName || !serviceName.trim()) return;

        const serviceType = prompt('请输入服务类型 (service/database/cache/queue/gateway，默认 service):') || 'service';

        try {
            // 通过添加拓扑关系 API 创建节点
            const result = await API.addTopologyDependency({
                source: serviceName.trim(),
                target: serviceName.trim()
            });

            if (result.success) {
                this.loadTopology();
            } else {
                alert('添加失败: ' + (result.message || '未知错误'));
            }
        } catch (error) {
            console.error('添加服务节点失败:', error);
            alert('添加失败: ' + error.message);
        }
    },

    /**
     * 添加依赖关系
     */
    async addDependency() {
        // 获取可用节点
        if (this.nodes.length < 2) {
            alert('节点数量不足，请先添加服务');
            return;
        }

        // 构建选择对话框
        let sourceOptions = '<option value="">选择源服务...</option>';
        let targetOptions = '<option value="">选择目标服务...</option>';

        this.nodes.forEach(node => {
            sourceOptions += '<option value="' + node.id + '">' + node.name + '</option>';
            targetOptions += '<option value="' + node.id + '">' + node.name + '</option>';
        });

        const dialogHtml = '<div style="padding: 1rem;">' +
            '<div style="margin-bottom: 1rem;">' +
            '<label style="display: block; margin-bottom: 0.5rem;">源服务</label>' +
            '<select id="depSource" style="width: 100%; padding: 0.5rem;">' + sourceOptions + '</select>' +
            '</div>' +
            '<div style="margin-bottom: 1rem;">' +
            '<label style="display: block; margin-bottom: 0.5rem;">目标服务</label>' +
            '<select id="depTarget" style="width: 100%; padding: 0.5rem;">' + targetOptions + '</select>' +
            '</div>' +
            '<div style="margin-bottom: 1rem;">' +
            '<label style="display: block; margin-bottom: 0.5rem;">依赖类型</label>' +
            '<select id="depType" style="width: 100%; padding: 0.5rem;">' +
            '<option value="depends_on">依赖</option>' +
            '<option value="calls">调用</option>' +
            '<option value="uses">使用</option>' +
            '</select>' +
            '</div>' +
            '</div>';

        // 使用自定义对话框
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.innerHTML = '<div class="modal-content" style="max-width: 400px;">' +
            '<div class="modal-header"><h3>添加依赖关系</h3></div>' +
            '<div class="modal-body">' + dialogHtml + '</div>' +
            '<div class="modal-footer">' +
            '<button class="btn" onclick="this.closest(\'.modal\').remove()">取消</button>' +
            '<button class="btn btn-primary" id="confirmAddDep">添加</button>' +
            '</div>' +
            '</div>';

        document.body.appendChild(modal);

        // 绑定确认按钮
        document.getElementById('confirmAddDep').addEventListener('click', async () => {
            const source = document.getElementById('depSource').value;
            const target = document.getElementById('depTarget').value;
            const type = document.getElementById('depType').value;

            if (!source || !target) {
                alert('请选择源服务和目标服务');
                return;
            }

            if (source === target) {
                alert('源服务和目标服务不能相同');
                return;
            }

            modal.remove();

            try {
                const result = await API.addTopologyDependency({
                    source: source,
                    target: target,
                    type: type
                });

                if (result.success) {
                    alert('✅ 依赖关系添加成功！');
                    this.loadTopology();
                } else {
                    alert('❌ 添加失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('添加依赖失败:', error);
                alert('❌ 请求失败: ' + error.message);
            }
        });
    },

    /**
     * 删除依赖关系
     * @param {string} source - 源服务 ID
     * @param {string} target - 目标服务 ID
     */
    async deleteDependency(source, target) {
        if (!confirm('确定要删除这条依赖关系吗？')) {
            return;
        }

        try {
            const result = await API.deleteTopologyDependency(source, target);

            if (result.success) {
                alert('✅ 依赖关系已删除！');
                this.loadTopology();
            } else {
                alert('❌ 删除失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('删除依赖失败:', error);
            alert('❌ 请求失败: ' + error.message);
        }
    },

    /**
     * 自动发现拓扑
     */
    async discoverTopology() {
        const btn = document.getElementById('discoverTopologyBtn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '发现中...';
        }

        try {
            const result = await API.discoverTopology();

            if (result.success) {
                alert('✅ 拓扑发现完成！发现 ' + (result.data.nodes ? result.data.nodes.length : 0) + ' 个节点');
                this.loadTopology();
            } else {
                alert('❌ 发现失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('拓扑发现失败:', error);
            alert('❌ 请求失败: ' + error.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '自动发现';
            }
        }
    }
};
