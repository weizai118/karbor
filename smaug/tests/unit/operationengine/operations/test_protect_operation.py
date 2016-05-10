#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from smaug import exception
from smaug.services.operationengine.operations import protect_operation
from smaug.tests import base


class ProtectOperationTestCase(base.TestCase):
    """Test cases for ProtectOperation class."""

    def test_check_operation_definition(self):
        operation = protect_operation.ProtectOperation()
        self.assertRaises(exception.InvalidOperationDefinition,
                          operation.check_operation_definition,
                          {})

    def test_execute(self):
        # TODO(chenzeng): mock the "create checkpoint" interface to
        # test execute function
        pass
