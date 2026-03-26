/**
 * 预测分析模块
 * 处理告警预测和模式分析功能
 */

const PredictionsModule = {
    predictions: [],
    patterns: [],

    /**
     * 初始化模块
     */
    init() {
        this.loadPredictions();
        this.loadPatterns();
        this.bindEvents();
    },

    /**
     * 绑定事件
     */
    bindEvents() {
        // 运行预测按钮
        const runPredictionBtn = document.getElementById('runPredictionBtn');
        if (runPredictionBtn) {
            runPredictionBtn.addEventListener('click', () => this.runPrediction());
        }

        // 分析模式按钮
        const analyzePatternsBtn = document.getElementById('analyzePatternsBtn');
        if (analyzePatternsBtn) {
            analyzePatternsBtn.addEventListener('click', () => this.analyzePatterns());
        }
    },

    /**
     * 加载预测列表
     */
    async loadPredictions() {
        try {
            const result = await API.getPredictions();

            if (result.success && result.data) {
                this.predictions = result.data.predictions || [];
                this.renderPredictions(this.predictions);
            } else {
                this.renderEmptyPredictions();
            }
        } catch (error) {
            console.error('加载预测失败:', error);
            this.renderEmptyPredictions();
        }
    },

    /**
     * 渲染预测列表
     * @param {array} predictions - 预测数据数组
     */
    renderPredictions(predictions) {
        const container = document.getElementById('predictionsList');
        if (!container) return;

        if (predictions.length === 0) {
            this.renderEmptyPredictions();
            return;
        }

        let html = '<div class="predictions-list">';

        predictions.forEach((prediction) => {
            const confidence = prediction.confidence || 0;
            const confidenceClass = confidence > 0.7 ? 'high' : (confidence > 0.4 ? 'medium' : 'low');

            html += '<div class="prediction-card">';
            html += '<div class="prediction-header">';
            html += '<span class="prediction-title">' + (prediction.title || '预测') + '</span>';
            html += '<span class="prediction-confidence ' + confidenceClass + '">' + Math.round(confidence * 100) + '% 置信度</span>';
            html += '</div>';

            html += '<div class="prediction-body">';
            html += '<p class="prediction-desc">' + (prediction.description || '') + '</p>';

            if (prediction.affected_services && prediction.affected_services.length > 0) {
                html += '<div class="prediction-services">';
                html += '<span class="label">影响服务:</span> ';
                prediction.affected_services.forEach((service, idx) => {
                    html += '<span class="service-tag">' + service + '</span>';
                    if (idx < prediction.affected_services.length - 1) {
                        html += ' ';
                    }
                });
                html += '</div>';
            }

            if (prediction.estimated_time) {
                html += '<div class="prediction-time">';
                html += '<span class="label">预计时间:</span> ' + formatTime(prediction.estimated_time);
                html += '</div>';
            }
            html += '</div>';

            html += '<div class="prediction-footer">';
            html += '<span class="prediction-timeago">' + timeAgo(prediction.created_at) + '</span>';
            html += '</div>';

            html += '</div>';
        });

        html += '</div>';
        container.innerHTML = html;
    },

    /**
     * 渲染空预测状态
     */
    renderEmptyPredictions() {
        const container = document.getElementById('predictionsList');
        if (!container) return;

        container.innerHTML = '<div class="empty-state"><div class="empty-icon">🔮</div><div class="empty-title">暂无预测</div><div class="empty-text">点击"运行预测"按钮开始分析</div></div>';
    },

    /**
     * 运行预测分析
     */
    async runPrediction() {
        const btn = document.getElementById('runPredictionBtn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '分析中...';
        }

        try {
            const result = await API.runPrediction();

            if (result.success) {
                alert('✅ 预测分析完成！');
                this.loadPredictions();
            } else {
                alert('❌ 预测失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('运行预测失败:', error);
            alert('❌ 请求失败: ' + error.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '运行预测';
            }
        }
    },

    /**
     * 加载模式分析结果
     */
    async loadPatterns() {
        try {
            const result = await API.getPatterns();

            if (result.success && result.data) {
                this.patterns = result.data.patterns || [];
                this.renderPatterns(this.patterns);
            }
        } catch (error) {
            console.error('加载模式失败:', error);
        }
    },

    /**
     * 渲染模式分析结果
     * @param {array} patterns - 模式数据数组
     */
    renderPatterns(patterns) {
        const container = document.getElementById('patternsList');
        if (!container) return;

        if (patterns.length === 0) {
            container.innerHTML = '<div class="empty-text">暂无模式数据</div>';
            return;
        }

        let html = '<div class="patterns-list">';

        patterns.forEach((pattern) => {
            html += '<div class="pattern-item">';
            html += '<div class="pattern-name">' + (pattern.name || '未命名模式') + '</div>';
            html += '<div class="pattern-stats">';
            html += '<span>出现次数: ' + (pattern.occurrence_count || 0) + '</span>';
            if (pattern.frequency) {
                html += '<span>频率: ' + pattern.frequency + '</span>';
            }
            html += '</div>';

            if (pattern.related_alerts && pattern.related_alerts.length > 0) {
                html += '<div class="pattern-alerts">';
                html += '<span class="label">关联告警:</span> ';
                pattern.related_alerts.forEach((alert, idx) => {
                    html += '<span class="alert-tag">#' + alert + '</span>';
                    if (idx < pattern.related_alerts.length - 1) {
                        html += ' ';
                    }
                });
                html += '</div>';
            }

            html += '</div>';
        });

        html += '</div>';
        container.innerHTML = html;
    },

    /**
     * 分析告警模式
     */
    async analyzePatterns() {
        const btn = document.getElementById('analyzePatternsBtn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '分析中...';
        }

        try {
            const result = await API.analyzePatterns();

            if (result.success) {
                alert('✅ 模式分析完成！');
                this.loadPatterns();
            } else {
                alert('❌ 分析失败: ' + (result.error || '未知错误'));
            }
        } catch (error) {
            console.error('分析模式失败:', error);
            alert('❌ 请求失败: ' + error.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '分析模式';
            }
        }
    }
};
