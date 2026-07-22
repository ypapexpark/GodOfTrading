#!/usr/bin/env ruby

require "fileutils"
require "xcodeproj"

root = File.expand_path("..", __dir__)
project_path = File.join(root, "LinguaFlowIOS.xcodeproj")
FileUtils.rm_rf(project_path)

project = Xcodeproj::Project.new(project_path)
project.root_object.attributes["LastSwiftUpdateCheck"] = "2660"
project.root_object.attributes["LastUpgradeCheck"] = "2660"

app_target = project.new_target(:application, "LinguaFlowIOS", :ios, "26.0")
keyboard_target = project.new_target(:app_extension, "LinguaFlowKeyboard", :ios, "26.0")
test_target = project.new_target(:unit_test_bundle, "LinguaFlowIOSTests", :ios, "26.0")

shared_group = project.main_group.new_group("Shared", "Shared")
app_group = project.main_group.new_group("LinguaFlowApp", "LinguaFlowApp")
keyboard_group = project.main_group.new_group("LinguaFlowKeyboard", "LinguaFlowKeyboard")
tests_group = project.main_group.new_group("LinguaFlowIOSTests", "LinguaFlowIOSTests")

shared_files = %w[
  TransformModels.swift
  SentenceExtractor.swift
  OnDeviceTextEngine.swift
].map { |name| shared_group.new_file(name) }

app_files = %w[
  LinguaFlowApp.swift
  ContentView.swift
].map { |name| app_group.new_file(name) }

keyboard_files = [keyboard_group.new_file("KeyboardViewController.swift")]
test_files = [tests_group.new_file("SentenceExtractorTests.swift")]

app_target.add_file_references(shared_files + app_files)
keyboard_target.add_file_references(shared_files + keyboard_files)
test_target.add_file_references(test_files)

app_target.add_dependency(keyboard_target)
test_target.add_dependency(app_target)

embed_extensions = app_target.new_copy_files_build_phase("Embed App Extensions")
embed_extensions.symbol_dst_subfolder_spec = :plug_ins
embed_extensions.add_file_reference(keyboard_target.product_reference)

project.build_configurations.each do |configuration|
  configuration.build_settings["SWIFT_VERSION"] = "6.0"
  configuration.build_settings["IPHONEOS_DEPLOYMENT_TARGET"] = "26.0"
  configuration.build_settings["CLANG_ENABLE_MODULES"] = "YES"
end

app_target.build_configurations.each do |configuration|
  configuration.build_settings.merge!({
    "PRODUCT_BUNDLE_IDENTIFIER" => "com.linguaflow.ios",
    "PRODUCT_NAME" => "LinguaFlow",
    "PRODUCT_MODULE_NAME" => "LinguaFlowIOS",
    "INFOPLIST_FILE" => "LinguaFlowApp/Info.plist",
    "GENERATE_INFOPLIST_FILE" => "NO",
    "TARGETED_DEVICE_FAMILY" => "1,2",
    "SWIFT_EMIT_LOC_STRINGS" => "YES",
    "CODE_SIGN_STYLE" => "Automatic",
    "CURRENT_PROJECT_VERSION" => "1",
    "MARKETING_VERSION" => "0.1.0",
    "ASSETCATALOG_COMPILER_APPICON_NAME" => "",
  })
end

keyboard_target.build_configurations.each do |configuration|
  configuration.build_settings.merge!({
    "PRODUCT_BUNDLE_IDENTIFIER" => "com.linguaflow.ios.keyboard",
    "PRODUCT_NAME" => "LinguaFlowKeyboard",
    "PRODUCT_MODULE_NAME" => "LinguaFlowKeyboard",
    "INFOPLIST_FILE" => "LinguaFlowKeyboard/Info.plist",
    "GENERATE_INFOPLIST_FILE" => "NO",
    "TARGETED_DEVICE_FAMILY" => "1,2",
    "SKIP_INSTALL" => "YES",
    "APPLICATION_EXTENSION_API_ONLY" => "YES",
    "CODE_SIGN_STYLE" => "Automatic",
    "CURRENT_PROJECT_VERSION" => "1",
    "MARKETING_VERSION" => "0.1.0",
  })
end

test_target.build_configurations.each do |configuration|
  configuration.build_settings.merge!({
    "PRODUCT_BUNDLE_IDENTIFIER" => "com.linguaflow.ios.tests",
    "PRODUCT_NAME" => "LinguaFlowIOSTests",
    "PRODUCT_MODULE_NAME" => "LinguaFlowIOSTests",
    "GENERATE_INFOPLIST_FILE" => "YES",
    "TARGETED_DEVICE_FAMILY" => "1,2",
    "TEST_HOST" => "$(BUILT_PRODUCTS_DIR)/LinguaFlow.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/LinguaFlow",
    "BUNDLE_LOADER" => "$(TEST_HOST)",
    "CODE_SIGN_STYLE" => "Automatic",
  })
end

project.save

scheme = Xcodeproj::XCScheme.new
scheme.add_build_target(app_target)
scheme.add_build_target(keyboard_target)
scheme.add_test_target(test_target)
scheme.set_launch_target(app_target)
scheme.save_as(project_path, "LinguaFlowIOS", true)

puts "Generated #{project_path}"
