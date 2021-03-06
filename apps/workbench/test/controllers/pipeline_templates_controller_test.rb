# Copyright (C) The Arvados Authors. All rights reserved.
#
# SPDX-License-Identifier: AGPL-3.0

require 'test_helper'

class PipelineTemplatesControllerTest < ActionController::TestCase
  test "component rendering copes with unexpeceted components format" do
    get(:show,
        params: {id: api_fixture("pipeline_templates")["components_is_jobspec"]["uuid"]},
        session: session_for(:active))
    assert_response :success
  end
end
