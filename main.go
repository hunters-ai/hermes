package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/tanmay-bhat/alterdeck/config"
)

var (
	incomingRequests = promauto.NewCounter(prometheus.CounterOpts{
		Name: "alterdeck_incoming_requests_total",
		Help: "The total number of incoming alert requests",
	})
	webhookRequests = promauto.NewCounter(prometheus.CounterOpts{
		Name: "alterdeck_webhook_requests_total",
		Help: "The total number of webhook requests sent",
	})
	webhookErrors = promauto.NewCounter(prometheus.CounterOpts{
		Name: "alterdeck_webhook_errors_total",
		Help: "The total number of webhook request errors",
	})
	processingDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "alterdeck_processing_duration_seconds",
		Help:    "The time taken to process alert requests",
		Buckets: prometheus.DefBuckets,
	})
	alertsReceivedByType = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "alterdeck_alerts_received_total",
			Help: "The total number of alerts received by type",
		},
		[]string{"alert_type"},
	)
	processingErrors = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "alterdeck_processing_errors_total",
			Help: "The total number of errors during alert processing",
		},
		[]string{"error_type", "alert_type"},
	)
	rundeckJobTriggers = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "alterdeck_rundeck_job_triggers_total",
			Help: "The total number of Rundeck job triggers",
		},
		[]string{"alert_type", "status"},
	)
)

type AlertPayload struct {
	Receiver string `json:"receiver"`
	Status   string `json:"status"`
	Alerts   []struct {
		Status string `json:"status"`
		Labels struct {
			Alertname string `json:"alertname"`
			Severity  string `json:"severity"`
		} `json:"labels"`
		StartsAt     time.Time `json:"startsAt"`
		EndsAt       time.Time `json:"endsAt"`
		GeneratorURL string    `json:"generatorURL"`
		Fingerprint  string    `json:"fingerprint"`
	} `json:"alerts"`
	CommonLabels map[string]string `json:"commonLabels"`
	ExternalURL  string            `json:"externalURL"`
	Version      string            `json:"version"`
	GroupKey     string            `json:"groupKey"`
}

type AlertProcessor struct {
	cfg *config.Config
}

func NewAlertProcessor(configPath string) (*AlertProcessor, error) {
	cfg, err := config.LoadAlertConfig(configPath)
	if err != nil {
		return nil, fmt.Errorf("loading config: %w", err)
	}

	return &AlertProcessor{
		cfg: cfg,
	}, nil
}

func (p *AlertProcessor) ProcessAlert(alert AlertPayload) (map[string]interface{}, error) {
	payloadMap := make(map[string]interface{})

	payloadBytes, _ := json.Marshal(alert)
	json.Unmarshal(payloadBytes, &payloadMap)

	var alertName string

	if labels, exists := payloadMap["commonLabels"].(map[string]interface{}); exists {
		if name, exists := labels["alertname"]; exists {
			alertName = name.(string)
		}
	}

	if alertName == "" && len(alert.Alerts) > 0 {
		alertName = alert.Alerts[0].Labels.Alertname
	}

	if alertName == "" {
		processingErrors.WithLabelValues("missing_alertname", "unknown").Inc()

		log.Printf("Alert name not found. Available payload structure:\n")
		for key, _ := range payloadMap {
			log.Printf("- %s\n", key)
		}

		if commonLabels, ok := payloadMap["commonLabels"].(map[string]interface{}); ok {
			commonLabelsJSON, _ := json.MarshalIndent(commonLabels, "", "  ")
			log.Printf("Contents of commonLabels:\n%s", string(commonLabelsJSON))
		}

		return nil, fmt.Errorf("alertname not found in payload")
	}

	alertsReceivedByType.WithLabelValues(alertName).Inc()

	alertConfig, exists := p.cfg.GetAlertConfig(alertName)
	if !exists {
		processingErrors.WithLabelValues("unknown_alert_type", alertName).Inc()
		return nil, fmt.Errorf("no configuration found for alert: %s", alertName)
	}

	result := make(map[string]interface{})
	missingFields := false
	var missing []string

	var sourceMap map[string]interface{}

	fieldsLocation := alertConfig.FieldsLocation
	if fieldsLocation == "" {
		fieldsLocation = "commonLabels"
	}

	if fieldsLocation == "root" {
		sourceMap = payloadMap
	} else if source, ok := payloadMap[fieldsLocation].(map[string]interface{}); ok {
		sourceMap = source
	} else {
		log.Printf("Warning: Fields location '%s' not found in payload for alert '%s'",
			fieldsLocation, alertName)

		log.Printf("Available payload locations for alert '%s':", alertName)
		for key, _ := range payloadMap {
			log.Printf("- %s", key)
		}

		sourceMap = make(map[string]interface{})
	}

	for _, field := range alertConfig.RequiredFields {
		if value, ok := sourceMap[field]; ok {
			result[field] = value
			continue
		}

		log.Printf("Required field '%s' not found in payload location '%s' for alert '%s'",
			field, fieldsLocation, alertName)
		missingFields = true
		missing = append(missing, field)
	}

	if missingFields {
		processingErrors.WithLabelValues("missing_fields", alertName).Inc()

		sourceJSON, _ := json.MarshalIndent(sourceMap, "", "  ")
		log.Printf("Missing required fields %v for alert '%s'. Available fields in '%s':\n%s",
			missing, alertName, fieldsLocation, string(sourceJSON))
	}

	return result, nil
}
func (p *AlertProcessor) SendToWebhook(alertName string, payload map[string]interface{}, alertTime time.Time) error {
	alertConfig, exists := p.cfg.GetAlertConfig(alertName)
	if !exists {
		processingErrors.WithLabelValues("config_not_found", alertName).Inc()
		return fmt.Errorf("no configuration found for alert: %s", alertName)
	}

	webhookURL := p.cfg.GetWebhookURL(alertConfig.JobID)

	rundeckPayload := map[string]interface{}{
		"options": payload,
	}

	jsonData, err := json.Marshal(rundeckPayload)
	if err != nil {
		processingErrors.WithLabelValues("json_marshal", alertName).Inc()
		return fmt.Errorf("marshaling payload: %w", err)
	}

	req, err := http.NewRequest("POST", webhookURL, bytes.NewBuffer(jsonData))
	if err != nil {
		processingErrors.WithLabelValues("create_request", alertName).Inc()
		return fmt.Errorf("creating request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Rundeck-Auth-Token", p.cfg.AuthToken)

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		webhookErrors.Inc()
		rundeckJobTriggers.WithLabelValues(alertName, "error").Inc()
		return fmt.Errorf("sending to webhook: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		processingErrors.WithLabelValues("read_response", alertName).Inc()
		return fmt.Errorf("reading response: %w", err)
	}

	if resp.StatusCode >= 400 {
		webhookErrors.Inc()
		rundeckJobTriggers.WithLabelValues(alertName, "failed").Inc()
		return fmt.Errorf("webhook returned error: %s - %s", resp.Status, string(body))
	}

	webhookRequests.Inc()
	rundeckJobTriggers.WithLabelValues(alertName, "success").Inc()

	return nil
}

func truncateString(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}

func main() {
	processor, err := NewAlertProcessor("config/config.yaml")
	if err != nil {
		log.Fatalf("Failed to initialize alert processor: %v", err)
	}
	r := gin.Default()

	r.Use(func(c *gin.Context) {
		if c.Request.URL.Path == "/api/v1/alerts" {
			start := time.Now()
			c.Next()
			duration := time.Since(start)
			processingDuration.Observe(duration.Seconds())
		} else {
			c.Next()
		}
	})

	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	r.POST("/api/v1/alerts", func(c *gin.Context) {
		incomingRequests.Inc()

		var alert AlertPayload
		if err := c.ShouldBindJSON(&alert); err != nil {
			log.Printf("Error parsing alert payload: %v", err)
			processingErrors.WithLabelValues("invalid_json", "unknown").Inc()

			rawData, _ := io.ReadAll(c.Request.Body)
			if len(rawData) > 0 {
				log.Printf("Failed to parse JSON payload, raw content (truncated to 1000 chars):\n%s",
					truncateString(string(rawData), 1000))
			}

			c.JSON(http.StatusBadRequest, gin.H{
				"error": "Invalid alert payload",
			})
			return
		}

		processedAlert, err := processor.ProcessAlert(alert)
		if err != nil {
			log.Printf("Error processing alert: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{
				"error": fmt.Sprintf("Error processing alert: %v", err),
			})
			return
		}

		alertName := "unknown"
		payloadBytes, _ := json.Marshal(alert)
		var payloadMap map[string]interface{}
		json.Unmarshal(payloadBytes, &payloadMap)

		if labels, exists := payloadMap["commonLabels"].(map[string]interface{}); exists {
			if name, exists := labels["alertname"]; exists {
				alertName = name.(string)
			}
		}

		if alertName == "unknown" && len(alert.Alerts) > 0 {
			alertName = alert.Alerts[0].Labels.Alertname
		}

		log.Printf("Received alert: %s", alertName)

		if os.Getenv("DEBUG") == "true" {
			payloadJSON, _ := json.MarshalIndent(alert, "", "  ")
			log.Printf("Full alert payload for '%s':\n%s", alertName, string(payloadJSON))
		}

		alertConfig, exists := processor.cfg.GetAlertConfig(alertName)
		if !exists {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": fmt.Sprintf("No configuration found for alert: %s", alertName),
			})
			return
		}

		missingFields := []string{}
		for _, field := range alertConfig.RequiredFields {
			if _, exists := processedAlert[field]; !exists {
				missingFields = append(missingFields, field)
			}
		}

		if len(missingFields) > 0 {
			errorMsg := fmt.Sprintf("Missing required fields for Rundeck job: %v", missingFields)
			log.Printf(errorMsg)
			c.JSON(http.StatusBadRequest, gin.H{
				"error":            errorMsg,
				"available_fields": processedAlert,
			})
			return
		}

		alertTime := time.Now()
		if len(alert.Alerts) > 0 && !alert.Alerts[0].StartsAt.IsZero() {
			alertTime = alert.Alerts[0].StartsAt
		}

		err = processor.SendToWebhook(alertName, processedAlert, alertTime)
		if err != nil {
			log.Printf("Error sending to webhook: %v", err)

			processedJSON, _ := json.MarshalIndent(processedAlert, "", "  ")
			log.Printf("Failed to send processed payload for alert '%s':\n%s", alertName, string(processedJSON))

			c.JSON(http.StatusInternalServerError, gin.H{
				"error": fmt.Sprintf("Error sending to webhook: %v", err),
			})
			return
		}

		log.Printf("Sent alert to webhook: %s", alertName)

		c.JSON(http.StatusOK, gin.H{
			"status":  "success",
			"message": "Alert processed and sent to webhook",
		})
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	log.Printf("Starting server on port %s", port)
	if err := r.Run(":" + port); err != nil {
		log.Fatalf("Error starting server: %v", err)
	}
}
