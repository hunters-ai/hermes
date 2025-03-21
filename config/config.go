package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

type Config struct {
	AuthToken    string                 `yaml:"auth_token"`
	BaseURL      string                 `yaml:"base_url"`
	AlertConfigs map[string]AlertConfig `yaml:"alerts"`
	PagerDuty    PagerDutyConfig        `yaml:"pagerduty"`
}

type PagerDutyConfig struct {
	Enabled      bool   `yaml:"enabled"`
	APIToken     string `yaml:"api_token"`
	TimeRangeMin int    `yaml:"time_range_min"`
	UpdateNotes  bool   `yaml:"update_notes"`
	FromEmail    string `yaml:"from_email"`
}

type AlertConfig struct {
	RequiredFields []string `yaml:"required_fields"`
	JobID          string   `yaml:"job_id"`
	FieldsLocation string   `yaml:"fields_location"`
}

func LoadAlertConfig(filename string) (*Config, error) {
	data, err := os.ReadFile(filename)
	if err != nil {
		return nil, fmt.Errorf("reading config file: %w", err)
	}

	var config Config
	if err := yaml.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("parsing config file: %w", err)
	}

	for alertName, alertConfig := range config.AlertConfigs {
		if alertConfig.FieldsLocation == "" {
			alertConfig.FieldsLocation = "commonLabels"
			config.AlertConfigs[alertName] = alertConfig
		}
	}

	if config.PagerDuty.TimeRangeMin == 0 {
		config.PagerDuty.TimeRangeMin = 15
	}

	return &config, nil
}

func (c *Config) GetAlertConfig(alertName string) (AlertConfig, bool) {
	alertConfig, exists := c.AlertConfigs[alertName]
	return alertConfig, exists
}

func (c *Config) GetWebhookURL(jobID string) string {
	return fmt.Sprintf("%s/api/52/job/%s/run", c.BaseURL, jobID)
}
